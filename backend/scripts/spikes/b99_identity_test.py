#!/usr/bin/env python3
"""Run synthetic, non-deployment B99 identity checks against the isolated dev stack.

Uses `cei-project` in us-east-1. Never prints or writes tokens, secret values,
request bodies, or Lambda error payloads. Results do not approve production use.
"""
from __future__ import annotations

import base64
import json
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import boto3
from botocore.exceptions import ClientError

from s2_scope_test import mcp

REGION = "us-east-1"
STACK = "IdentitySpikeStack"
USER_A = "00000000-0000-4000-8000-000000000001"
USER_B = "00000000-0000-4000-8000-000000000002"
SESSION_A = "00000000-0000-4000-8000-000000000011"
SESSION_B = "00000000-0000-4000-8000-000000000012"
TOOL = "identity-probe___probe"


def mint(outputs: dict[str, str], secret: str, user: str | None, session: str | None) -> str:
    fields = {"grant_type": "client_credentials", "scope": "cei-spike/probe"}
    if user is not None and session is not None:
        fields["aws_client_metadata"] = json.dumps({"cei_user_sub": user, "cei_session_id": session})
    basic = base64.b64encode(f"{outputs['ClientId']}:{secret}".encode()).decode()
    request = urllib.request.Request(outputs["TokenEndpoint"], data=urllib.parse.urlencode(fields).encode(),
                                     headers={"Authorization": f"Basic {basic}",
                                              "Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)["access_token"]


def identity(response: dict[str, Any]) -> dict[str, str] | None:
    body = response.get("json")
    if response.get("status") != 200 or not isinstance(body, dict) or "error" in body:
        return None
    result = body.get("result")
    if not isinstance(result, dict) or result.get("isError"):
        return None
    candidate = result.get("structuredContent")
    if not isinstance(candidate, dict):
        blocks = result.get("content") or []
        candidate = None
        for block in blocks:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                try:
                    candidate = json.loads(block["text"])
                    break
                except ValueError:
                    continue
    return candidate.get("identity") if isinstance(candidate, dict) else None


def denied(response: dict[str, Any]) -> bool:
    body = response.get("json")
    return (response.get("status") in (401, 403)
            or isinstance(body, dict) and isinstance(body.get("error"), dict)
            and body["error"].get("code") == -32001)


def main() -> None:
    session = boto3.Session(profile_name="cei-project", region_name=REGION)
    if session.client("sts").get_caller_identity()["Account"] != "257137551728":
        raise RuntimeError("Unexpected account")
    stack = session.client("cloudformation").describe_stacks(StackName=STACK)["Stacks"][0]
    outputs = {item["OutputKey"]: item["OutputValue"] for item in stack["Outputs"]}
    secret = session.client("secretsmanager").get_secret_value(SecretId=outputs["ClientSecretArn"])["SecretString"]
    token_a = mint(outputs, secret, USER_A, SESSION_A)
    token_b = mint(outputs, secret, USER_B, SESSION_B)
    no_claims = mint(outputs, secret, None, None)
    refreshed = mint(outputs, secret, USER_A, SESSION_A)
    parts = token_a.split(".")
    midpoint = len(parts[2]) // 2
    parts[2] = parts[2][:midpoint] + ("A" if parts[2][midpoint] != "A" else "B") + parts[2][midpoint + 1:]
    invalid_signature = ".".join(parts)

    def invoke(token: str, forged: bool = False) -> dict[str, Any]:
        args: dict[str, Any] = {"query": "synthetic"}
        if forged:
            args["_cei_spike_identity"] = {"user_id": USER_B, "session_id": SESSION_B}
        return mcp(outputs["GatewayUrl"], token, "tools/call", {"name": TOOL, "arguments": args})

    checks = {
        "user_a": identity(invoke(token_a)) == {"user_id": USER_A, "session_id": SESSION_A},
        "user_b": identity(invoke(token_b)) == {"user_id": USER_B, "session_id": SESSION_B},
        "forged_context_overwritten": identity(invoke(token_a, forged=True)) == {"user_id": USER_A, "session_id": SESSION_A},
        "missing_claims_denied": denied(invoke(no_claims)),
        "invalid_signature_denied": denied(invoke(invalid_signature)),
        "refreshed_token_binding": identity(invoke(refreshed)) == {"user_id": USER_A, "session_id": SESSION_A},
    }
    with ThreadPoolExecutor(max_workers=2) as executor:
        pair = list(executor.map(invoke, [token_a, token_b]))
    checks["concurrent_user_isolation"] = [identity(item) for item in pair] == [
        {"user_id": USER_A, "session_id": SESSION_A}, {"user_id": USER_B, "session_id": SESSION_B}]
    creds = session.client("sts").assume_role(RoleArn=outputs["ProbeCallerRoleArn"], RoleSessionName="b99-denial-probe")["Credentials"]
    caller = boto3.client("lambda", region_name=REGION, aws_access_key_id=creds["AccessKeyId"],
                          aws_secret_access_key=creds["SecretAccessKey"], aws_session_token=creds["SessionToken"])
    try:
        caller.invoke(FunctionName=outputs["TargetArn"], Payload=b"{}")
        checks["direct_probe_role_denied"] = False
    except ClientError as exc:
        checks["direct_probe_role_denied"] = exc.response.get("Error", {}).get("Code") in ("AccessDenied", "AccessDeniedException")
    print(json.dumps({"stack": STACK, "checks": checks, "all_passed": all(checks.values()),
                      "unverified": ["expired/wrong-issuer/wrong-client token", "actual application role access",
                                     "production interceptor bypass boundaries"]}, indent=2))
    if not all(checks.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
