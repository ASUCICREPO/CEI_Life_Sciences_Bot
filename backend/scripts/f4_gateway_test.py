#!/usr/bin/env python3
"""F4 deployed check: the tool Gateway, its 10 source targets, scoped Router tokens and Cedar.

Stands in for the Session Router (F5) exactly like the S2 harness did: assumes the F4 placeholder
Router role (the only identity besides the token-refresh Lambda that can read the Router M2M secret),
mints client-credentials tokens, and calls the Gateway over MCP.

Checks
  token    all-scope token carries cei_user_sub / cei_session_id (pre-token trigger, V3_0);
           malformed client metadata makes Cognito refuse the token
  list     tools/list with the all-scope token shows all ten `<source>___search` tools
  call     every tool returns a DataSourceResult with at least one citation (live upstream APIs)
  scope    a PubMed-only token: PubMed allowed; another source allowed in LOG_ONLY, denied with
           JSON-RPC -32002 in ENFORCE; tools/list filtered in ENFORCE
  refresh  POST /internal/token-refresh signed by a non-allowlisted caller -> 403 (fails closed)
  logs     the Gateway log group received events

Tokens and the client secret are never printed. Stdlib + boto3.

Usage:  AWS_PROFILE=ciccei python3 backend/scripts/f4_gateway_test.py [--out results.json]
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

MCP_PROTOCOL_VERSION = "2025-03-26"  # S2: the only version the Gateway accepts
SOURCES = ["pubmed", "clinicaltrials", "biorxiv", "medrxiv", "chembl", "opentargets", "cmscoverage", "icd10", "npi", "pubchem"]
# Queries known to return results from each live API (checked during F4 chunk 1)
ARGS = {
    "npi": {"name": "john smith", "state": "AZ", "max_results": 2},
    "opentargets": {"target": "EGFR", "max_results": 2},
    "cmscoverage": {"query": "MRI", "max_results": 2},
}
DEFAULT_ARGS = {"query": "aspirin", "max_results": 2}
POLICY_DENY_CODE = -32002  # S2 question 4

RESULTS: list[dict[str, str]] = []


def record(section: str, check: str, ok: bool | None, detail: str = "") -> None:
    status = "PASS" if ok else ("INFO" if ok is None else "FAIL")
    RESULTS.append({"section": section, "check": check, "status": status, "detail": detail})
    print(f"  [{status}] {section}: {check}" + (f"  ({detail})" if detail and status != "PASS" else ""))


def outputs(cfn: Any, stack: str) -> dict[str, str]:
    data = cfn.describe_stacks(StackName=stack)["Stacks"][0]["Outputs"]
    return {o["OutputKey"]: o["OutputValue"] for o in data}


def jwt_claims(token: str) -> dict[str, Any]:
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def token_request(out: dict[str, str], secret: str, scopes: list[str], metadata: dict[str, Any] | None) -> tuple[int, dict]:
    form = {"grant_type": "client_credentials", "scope": " ".join(scopes)}
    if metadata is not None:
        form["aws_client_metadata"] = json.dumps(metadata)
    basic = base64.b64encode(f"{out['RouterClientId']}:{secret}".encode()).decode()
    req = urllib.request.Request(
        out["ToolTokenEndpoint"],
        data=urllib.parse.urlencode(form).encode(),
        headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, {"raw": body[:300]}


_rpc_id = 0


def mcp(url: str, token: str, method: str, params: dict[str, Any] | None = None) -> tuple[int, Any]:
    global _rpc_id
    _rpc_id += 1
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": _rpc_id, "method": method}
    if params is not None:
        body["params"] = params
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            status, text = resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        status, text = exc.code, exc.read().decode("utf-8", "replace")
    if "data:" in text and not text.lstrip().startswith("{"):  # SSE framing
        datas = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
        text = datas[-1] if datas else ""
    try:
        return status, json.loads(text)
    except ValueError:
        return status, {"raw": text[:300]}


def call_tool(url: str, token: str, source: str) -> tuple[str, Any]:
    """('ok', DataSourceResult dict) | ('denied', error) | ('error', detail)."""
    status, body = mcp(url, token, "tools/call", {"name": f"{source}___search", "arguments": ARGS.get(source, DEFAULT_ARGS)})
    if isinstance(body, dict) and "error" in body:
        code = (body["error"] or {}).get("code")
        return ("denied" if code == POLICY_DENY_CODE else "error"), body["error"]
    result = (body or {}).get("result") if isinstance(body, dict) else None
    if status != 200 or not isinstance(result, dict) or result.get("isError"):
        return "error", f"HTTP {status} {str(body)[:200]}"
    text = next((c.get("text") for c in result.get("content", []) if c.get("type") == "text"), None)
    try:
        return "ok", json.loads(text) if text else result.get("structuredContent")
    except ValueError:
        return "error", f"non-JSON tool content: {str(text)[:200]}"


def listed(url: str, token: str) -> set[str]:
    _, body = mcp(url, token, "tools/list", {})
    tools = ((body or {}).get("result") or {}).get("tools") or [] if isinstance(body, dict) else []
    return {t.get("name", "") for t in tools}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stack", default="BackendStack")
    parser.add_argument("--region", default="us-east-2")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    base = boto3.Session(region_name=args.region)
    out = outputs(base.client("cloudformation"), args.stack)
    mode = out["ToolPolicyMode"]
    url = out["ToolGatewayUrl"]
    all_scopes = [s for s in out["ToolScopes"].split(",") if s]
    source_scopes = [s for s in all_scopes if "/src." in s and not s.endswith("src.ncbi")]
    common = [s for s in all_scopes if s.endswith("/common")]
    print(f"Gateway {out['ToolGatewayId']} in {args.region}, policy mode {mode}")

    creds = base.client("sts").assume_role(RoleArn=out["RouterRoleArn"], RoleSessionName="f4-gateway-test")["Credentials"]
    router = boto3.Session(
        aws_access_key_id=creds["AccessKeyId"], aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"], region_name=args.region,
    )
    secret = router.client("secretsmanager").get_secret_value(SecretId=out["RouterSecretArn"])["SecretString"]

    print("\n== token (pre-token trigger) ==")
    sub, session = str(uuid.uuid4()), f"f4test-{int(time.time())}"
    status, tok = token_request(out, secret, source_scopes + common, {"cei_user_sub": sub, "cei_session_id": session})
    record("token", "all-scope token issued", status == 200, f"HTTP {status} {tok if status != 200 else ''}")
    if status != 200:
        return 1
    full = tok["access_token"]
    claims = jwt_claims(full)
    record("token", "carries cei_user_sub and cei_session_id", claims.get("cei_user_sub") == sub and claims.get("cei_session_id") == session,
           str({k: claims.get(k) for k in ("cei_user_sub", "cei_session_id")}))
    record("token", "scopes are exactly the requested set", set(claims.get("scope", "").split()) == set(source_scopes + common))
    record("token", "1 h lifetime", claims.get("exp", 0) - claims.get("iat", 0) == 3600, str(claims.get("exp", 0) - claims.get("iat", 0)))
    status, body = token_request(out, secret, common, {"cei_user_sub": "not-a-uuid"})
    record("token", "malformed metadata -> token refused", status >= 400, f"HTTP {status}")
    status, tok_none = token_request(out, secret, common, None)
    record("token", "no metadata -> token issued without claims", status == 200 and "cei_user_sub" not in jwt_claims(tok_none.get("access_token", "a.e30.b")), f"HTTP {status}")

    print("\n== list + call (all-scope token) ==")
    _, init = mcp(url, full, "initialize", {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "f4-test", "version": "0"}})
    record("list", "initialize", isinstance(init, dict) and "result" in init, str(init)[:200])
    tools = listed(url, full)
    record("list", "tools/list shows all ten tools", tools == {f"{s}___search" for s in SOURCES}, str(sorted(tools)))
    for source in SOURCES:
        kind, payload = call_tool(url, full, source)
        ok = kind == "ok" and isinstance(payload, dict) and payload.get("citations") and not payload.get("query_metadata", {}).get("error")
        detail = f"{kind}: {str(payload)[:160]}" if not ok else ""
        record("call", f"{source}___search returns citations", bool(ok), detail)

    print(f"\n== scope (PubMed-only token, {mode}) ==")
    _, tok_pm = token_request(out, secret, [s for s in source_scopes if s.endswith("src.pubmed")], {"cei_user_sub": sub, "cei_session_id": session})
    pm = tok_pm["access_token"]
    kind, _ = call_tool(url, pm, "pubmed")
    record("scope", "PubMed-only token calls pubmed", kind == "ok", kind)
    kind, err = call_tool(url, pm, "chembl")
    if mode == "ENFORCE":
        record("scope", "PubMed-only token denied chembl (-32002)", kind == "denied", f"{kind} {str(err)[:160]}")
        pm_tools = listed(url, pm)
        record("scope", "tools/list shows only pubmed", pm_tools == {"pubmed___search"}, str(sorted(pm_tools)))
    else:
        record("scope", "LOG_ONLY: PubMed-only token still reaches chembl", kind == "ok", f"{kind} {str(err)[:160]}")
        record("scope", f"LOG_ONLY tools/list for PubMed-only token: {sorted(listed(url, pm))}", None)

    print("\n== refresh (fails closed until F5) ==")
    api = out["ApiBaseUrl"].rstrip("/") + "/internal/token-refresh"
    request = AWSRequest(method="POST", url=api, data=json.dumps({"session_id": session}), headers={"Content-Type": "application/json"})
    SigV4Auth(base.get_credentials(), "execute-api", args.region).add_auth(request)
    req = urllib.request.Request(api, data=request.body.encode() if isinstance(request.body, str) else request.body, headers=dict(request.headers), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = resp.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    record("refresh", "non-allowlisted signed caller -> 403", status == 403, f"HTTP {status}")

    print("\n== logs ==")
    logs = base.client("logs")
    time.sleep(30)
    events = logs.filter_log_events(logGroupName=out["ToolGatewayLogGroup"], startTime=int((time.time() - 1800) * 1000), limit=200).get("events", [])
    record("logs", f"Gateway log group received events ({len(events)})", len(events) > 0)
    decisions = [e for e in events if any(w in e["message"].lower() for w in ("polic", "deny", "denied", "authoriz"))]
    record("logs", f"events mentioning a policy decision: {len(decisions)}", None)

    failed = [r for r in RESULTS if r["status"] == "FAIL"]
    print(f"\n==== F4 Gateway check ({mode}): {sum(r['status'] == 'PASS' for r in RESULTS)} PASS, {len(failed)} FAIL ====")
    if args.out:
        with open(args.out, "w") as f:
            json.dump({"mode": mode, "results": RESULTS, "log_sample": [e["message"][:1500] for e in decisions[:10] or events[:10]]}, f, indent=2)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
