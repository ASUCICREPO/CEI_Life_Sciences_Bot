#!/usr/bin/env python3
"""S2 tool-scope spike test script (Policy in AgentCore / Cedar on an AgentCore Gateway).

FIXME: remove after S2 validation (B92)

Usage (AWS CloudShell, stdlib + boto3 only; CloudShell credentials must be allowed to
sts:AssumeRole on the two spike roles, and to read stack outputs):

    python3 s2_scope_test.py                       # mode comes from the stack output PolicyMode
    python3 s2_scope_test.py --out s2-enforce.json # choose the results file
    python3 s2_scope_test.py --wait-expiry         # also wait ~5 min for a token to expire (task 9)
    python3 s2_scope_test.py --skip-logs           # do not wait for / query the Gateway log group

Run order (see .agents/phases/findings/S2.md): deploy LOG_ONLY -> run -> redeploy with
--spike-policy-mode ENFORCE -> run -> destroy the stack.

What it checks (spike tasks 1-9):
  secret   The runner role reads the M2M secret; the runtime role and the caller do not (tasks 2, Q3).
  tokens   Tokens carry exactly the requested scopes (task 1).
  LOG_ONLY Every token, even one with no relevant scope, calls every target; denies are logged (task 5).
  ENFORCE  target-a token: calls A and shared, denied B, tools/list shows only A + shared (tasks 6, 7).
  deny     The raw deny error shape seen by a LangGraph-style node (task 8).
  refresh  IAM-authenticated refresh stub returns a same-scope token that works (task 9, B67).

Output: a PASS/FAIL table on stdout and a JSON results file. Tokens and the client secret are
never printed or written; only scopes and expiry times are recorded. Exit code 1 on any FAIL.
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
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.exceptions import ClientError

# ADR: 2025-03-26 | Rationale: the deployed AgentCore Gateway rejected 2025-11-25 with JSON-RPC -32600
# "Unsupported protocol version" (supported: ["2025-03-26"]) on 2026-10-28, which made every call look denied
MCP_PROTOCOL_VERSION = "2025-03-26"
TOOL = "echo"

RESULTS: list[dict[str, Any]] = []
RAW: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Result bookkeeping
# ---------------------------------------------------------------------------
def record(task: str, check: str, expected: str, observed: str, status: str) -> None:
    """status: PASS | FAIL | INFO | WARN (WARN = could not verify, not a failure)."""
    RESULTS.append({"task": task, "check": check, "expected": expected, "observed": observed, "status": status})


def check_eq(task: str, check: str, expected: Any, observed: Any) -> bool:
    ok = expected == observed
    record(task, check, str(expected), str(observed), "PASS" if ok else "FAIL")
    return ok


def print_table() -> None:
    rows = [("TASK", "CHECK", "EXPECTED", "OBSERVED", "RESULT")] + [
        (r["task"], r["check"], r["expected"], r["observed"], r["status"]) for r in RESULTS
    ]
    widths = [min(max(len(str(row[i])) for row in rows), cap) for i, cap in enumerate((6, 58, 34, 34, 6))]
    for n, row in enumerate(rows):
        print("  ".join(str(cell)[: widths[i]].ljust(widths[i]) for i, cell in enumerate(row)))
        if n == 0:
            print("  ".join("-" * w for w in widths))


# ---------------------------------------------------------------------------
# AWS helpers
# ---------------------------------------------------------------------------
def stack_outputs(region: str, stack: str) -> dict[str, str]:
    cfn = boto3.client("cloudformation", region_name=region)
    outputs = cfn.describe_stacks(StackName=stack)["Stacks"][0]["Outputs"]
    return {o["OutputKey"]: o["OutputValue"] for o in outputs}


def assume(role_arn: str, name: str, region: str) -> boto3.Session:
    creds = boto3.client("sts", region_name=region).assume_role(RoleArn=role_arn, RoleSessionName=name)["Credentials"]
    return boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=region,
    )


def read_secret(session: boto3.Session, secret_arn: str) -> str:
    return session.client("secretsmanager").get_secret_value(SecretId=secret_arn)["SecretString"]


# ---------------------------------------------------------------------------
# Tokens (Cognito client credentials)
# ---------------------------------------------------------------------------
def jwt_claims(token: str) -> dict[str, Any]:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def mint_token(out: dict[str, str], client_secret: str, scopes: list[str]) -> str:
    full = " ".join(f"{out['ResourceServerId']}/{s}" for s in scopes)
    basic = base64.b64encode(f"{out['ClientId']}:{client_secret}".encode()).decode()
    req = urllib.request.Request(
        out["TokenEndpoint"],
        data=urllib.parse.urlencode({"grant_type": "client_credentials", "scope": full}).encode(),
        headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())["access_token"]


def scope_set(token: str) -> set[str]:
    return set(jwt_claims(token).get("scope", "").split())


# ---------------------------------------------------------------------------
# Gateway MCP client (JSON-RPC over HTTP)
# ---------------------------------------------------------------------------
class GatewayCallError(Exception):
    """Raised by the strict client; mirrors what a LangGraph tool node would see."""

    def __init__(self, message: str, http_status: int | None, raw_body: str, jsonrpc_error: Any, is_error_result: bool):
        super().__init__(message)
        self.http_status = http_status
        self.raw_body = raw_body
        self.jsonrpc_error = jsonrpc_error
        self.is_error_result = is_error_result


_rpc_id = 0


def mcp(url: str, token: str, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """One MCP request. Never raises on HTTP errors; returns status, raw body and parsed JSON."""
    global _rpc_id
    _rpc_id += 1
    body = {"jsonrpc": "2.0", "id": _rpc_id, "method": method}
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
        with urllib.request.urlopen(req, timeout=60) as resp:
            status, text = resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        status, text = exc.code, exc.read().decode("utf-8", "replace")
    parsed: Any = None
    candidate = text
    if "data:" in text and not text.lstrip().startswith("{"):  # SSE framing
        datas = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
        candidate = datas[-1] if datas else ""
    try:
        parsed = json.loads(candidate)
    except ValueError:
        pass
    return {"status": status, "text": text, "json": parsed}


def call_strict(url: str, token: str, tool_name: str) -> dict[str, Any]:
    """Call a tool; raise GatewayCallError on any kind of denial or failure (LangGraph-node style)."""
    resp = mcp(url, token, "tools/call", {"name": tool_name, "arguments": {"message": "hello"}})
    parsed = resp["json"] if isinstance(resp["json"], dict) else {}
    result = parsed.get("result") if isinstance(parsed.get("result"), dict) else None
    if resp["status"] != 200 or "error" in parsed or result is None or result.get("isError"):
        raise GatewayCallError(
            f"tools/call {tool_name} failed (HTTP {resp['status']})",
            resp["status"],
            resp["text"],
            parsed.get("error"),
            bool(result and result.get("isError")),
        )
    return result


def allowed(url: str, token: str, tool_name: str) -> bool:
    try:
        call_strict(url, token, tool_name)
        return True
    except GatewayCallError:
        return False


def listed_tools(url: str, token: str) -> tuple[set[str], dict[str, Any]]:
    resp = mcp(url, token, "tools/list", {})
    parsed = resp["json"] if isinstance(resp["json"], dict) else {}
    tools = (parsed.get("result") or {}).get("tools") or []
    return {t.get("name", "") for t in tools}, resp


def fake_langgraph_node(state: dict[str, Any]) -> dict[str, Any]:
    """Plain function standing in for a LangGraph tool node: catch the error, keep its shape."""
    try:
        state["result"] = call_strict(state["url"], state["token"], state["tool"])
    except GatewayCallError as exc:
        state["error"] = {
            "exception_type": type(exc).__name__,
            "message": str(exc),
            "http_status": exc.http_status,
            "jsonrpc_error": exc.jsonrpc_error,
            "is_error_result": exc.is_error_result,
            "raw_body": exc.raw_body[:2000],
        }
    return state


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------
def check_secret_boundary(out: dict[str, str], region: str, runner: boto3.Session, runtime: boto3.Session) -> str:
    """Tasks 2 / Q3. Returns the client secret (read as the runner) for minting tokens."""
    secret_arn = out["ClientSecretArn"]
    secret = read_secret(runner, secret_arn)
    record("2", "runner role reads the M2M secret", "allowed", "allowed", "PASS")

    for label, session in (("runtime role", runtime), ("caller identity (CloudShell user)", boto3.Session(region_name=region))):
        try:
            read_secret(session, secret_arn)
            record("2", f"{label} reads the M2M secret", "AccessDenied", "allowed", "FAIL")
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            RAW[f"secret_deny_{label.split()[0]}"] = exc.response["Error"]["Message"]
            record("2", f"{label} reads the M2M secret", "AccessDenied", code, "PASS" if code == "AccessDeniedException" else "FAIL")
    return secret


def check_tokens(out: dict[str, str], secret: str) -> dict[str, str]:
    """Task 1: tokens carry exactly the requested scopes."""
    rs = out["ResourceServerId"]
    plans = {
        "a_only": ["target-a"],
        "b_only": ["target-b"],
        "a_shared": ["target-a", "shared"],
        "none": ["probe"],  # a scope no policy references: "no relevant scope"
        "all": ["target-a", "target-b", "shared"],
    }
    tokens: dict[str, str] = {}
    for name, scopes in plans.items():
        tokens[name] = mint_token(out, secret, scopes)
        want = {f"{rs}/{s}" for s in scopes}
        check_eq("1", f"token '{name}' carries exactly its requested scopes", sorted(want), sorted(scope_set(tokens[name])))
    RAW["token_claims"] = {
        n: {"scope": jwt_claims(t).get("scope"), "exp": jwt_claims(t).get("exp"), "client_id": jwt_claims(t).get("client_id")}
        for n, t in tokens.items()
    }
    names = out["ScopeNames"].split(",")
    collisions = [(a, b) for a in names for b in names if a != b and a in b]
    check_eq("1", "no scope name is a substring of another (B69)", [], collisions)
    return tokens


def check_handshake(url: str, tokens: dict[str, str]) -> None:
    init = mcp(url, tokens["none"], "initialize", {
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "s2-scope-test", "version": "0"},
    })
    ok = init["status"] == 200 and isinstance(init["json"], dict) and "result" in init["json"]
    RAW["initialize"] = {"status": init["status"], "body": init["text"][:1000]}
    record("4", "token with no relevant scope can initialize", "HTTP 200 + result", f"HTTP {init['status']}", "PASS" if ok else "FAIL")


def check_matrix(url: str, tokens: dict[str, str], mode: str) -> None:
    a, b, sh = (f"SpikeTarget{x}___{TOOL}" for x in ("A", "B", "Shared"))
    everything = {a, b, sh}

    if mode == "LOG_ONLY":
        # Task 5: nothing is blocked, even for a token with no relevant scope.
        for name in ("none", "a_only", "b_only"):
            for label, tool in (("A", a), ("B", b), ("shared", sh)):
                ok = allowed(url, tokens[name], tool)
                record("5", f"[{name}] calls {label}", "allowed (LOG_ONLY)", "allowed" if ok else "denied", "PASS" if ok else "FAIL")
        got, resp = listed_tools(url, tokens["none"])
        RAW["tools_list_none_log_only"] = resp["text"][:3000]
        check_eq("5", "[none] tools/list shows every tool (LOG_ONLY)", sorted(everything), sorted(got))
        return

    # ENFORCE expectations: scope -> allowed targets; shared is open to all (B70).
    expect = {
        "a_only": {a, sh},
        "b_only": {b, sh},
        "a_shared": {a, sh},
        "none": {sh},
        "all": {a, b, sh},
    }
    for name, want in expect.items():
        for label, tool in (("A", a), ("B", b), ("shared", sh)):
            ok = allowed(url, tokens[name], tool)
            task = "7" if label == "shared" else "6"
            record(task, f"[{name}] calls {label}", "allowed" if tool in want else "DENIED", "allowed" if ok else "DENIED",
                   "PASS" if ok == (tool in want) else "FAIL")
        got, resp = listed_tools(url, tokens[name])
        RAW[f"tools_list_{name}"] = resp["text"][:3000]
        check_eq("6", f"[{name}] tools/list shows only its tools", sorted(want), sorted(got))


def check_deny_shape(url: str, tokens: dict[str, str], mode: str) -> None:
    """Task 8: raw deny error as seen by a LangGraph-style node (a_only token calling target B)."""
    state = fake_langgraph_node({"url": url, "token": tokens["a_only"], "tool": f"SpikeTargetB___{TOOL}"})
    RAW["deny_shape"] = state.get("error") or {"not_denied": True, "mode": mode}
    if mode == "ENFORCE":
        err = state.get("error")
        obs = f"{err['exception_type']} http={err['http_status']} rpc={err['jsonrpc_error']}" if err else "no error"
        record("8", "denied call surfaces an error to the node (shape in JSON: deny_shape)", "error raised", obs, "PASS" if err else "FAIL")
    else:
        record("8", "LOG_ONLY: call to B is not denied (deny shape needs ENFORCE)", "no error", "no error" if "error" not in state else "error", "INFO")


def check_logs(out: dict[str, str], region: str, since_ms: int, wait: int, mode: str) -> None:
    """Task 5: denies are logged. Log format is undocumented, so a miss is WARN, not FAIL."""
    print(f"Waiting {wait}s for Gateway log delivery...", file=sys.stderr)
    time.sleep(wait)
    logs = boto3.client("logs", region_name=region)
    try:
        events = logs.filter_log_events(logGroupName=out["GatewayLogGroup"], startTime=since_ms, limit=200)["events"]
    except ClientError as exc:
        record("5", "Gateway log group readable", "readable", exc.response["Error"]["Code"], "WARN")
        return
    RAW["gateway_log_sample"] = [e["message"][:1500] for e in events[:25]]
    hits = [e for e in events if "SpikeTarget" in e["message"] and any(w in e["message"].lower() for w in ("deny", "denied", "forbid"))]
    expected = "deny decisions logged" if mode == "LOG_ONLY" else "deny decisions logged (info)"
    record("5", "Gateway logs contain policy deny decisions", expected, f"{len(hits)} matching of {len(events)} events",
           "PASS" if hits else "WARN")


def signed_post(url: str, session: boto3.Session | None, region: str, payload: dict[str, Any]) -> tuple[int, str]:
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if session is not None:
        req = AWSRequest(method="POST", url=url, data=body, headers=headers)
        SigV4Auth(session.get_credentials().get_frozen_credentials(), "lambda", region).add_auth(req)
        headers = dict(req.headers)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers, method="POST"), timeout=30) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def check_refresh(out: dict[str, str], region: str, runtime: boto3.Session, runner_secret: str, mode: str, wait_expiry: bool) -> None:
    """Task 9 / B67: IAM-authenticated refresh endpoint returns a same-scope token that works."""
    url = out["TokenRefreshUrl"]
    old = mint_token(out, runner_secret, ["target-a", "shared"])
    status, text = signed_post(url, runtime, region, {"token": old, "run_id": "run-1"})
    fresh = ""
    if status == 200:
        fresh = json.loads(text)["access_token"]
    record("9", "runtime role calls refresh stub (SigV4)", "HTTP 200", f"HTTP {status}", "PASS" if status == 200 else "FAIL")
    if fresh:
        check_eq("9", "refreshed token has the same scopes", sorted(scope_set(old)), sorted(scope_set(fresh)))
        record("9", "refreshed token is a new token", "different", "different" if fresh != old else "same", "PASS" if fresh != old else "FAIL")
        url_gw = out["GatewayUrl"]
        ok = allowed(url_gw, fresh, f"SpikeTargetA___{TOOL}")
        record("9", "refreshed token calls target A at the Gateway", "allowed", "allowed" if ok else "denied", "PASS" if ok else "FAIL")
        if mode == "ENFORCE":
            denied = not allowed(url_gw, fresh, f"SpikeTargetB___{TOOL}")
            record("9", "refreshed token is still denied target B", "DENIED", "DENIED" if denied else "allowed", "PASS" if denied else "FAIL")

    status, _ = signed_post(url, runtime, region, {"token": old, "run_id": "inactive"})
    record("9", "refresh refused for an inactive run", "HTTP 409", f"HTTP {status}", "PASS" if status == 409 else "FAIL")
    status, _ = signed_post(url, None, region, {"token": old, "run_id": "run-1"})
    record("9", "unsigned call to the refresh URL is rejected", "HTTP 403", f"HTTP {status}", "PASS" if status == 403 else "FAIL")

    if not wait_expiry:
        record("9", "old token expires mid-run (use --wait-expiry)", "401 after TTL", "skipped", "INFO")
        return
    short = mint_token(out, runner_secret, ["target-a"])
    exp = jwt_claims(short)["exp"]
    delay = max(0, exp - int(time.time())) + 15
    print(f"Waiting {delay}s for a token to expire...", file=sys.stderr)
    time.sleep(delay)
    resp = mcp(out["GatewayUrl"], short, "tools/list", {})
    record("9", "expired token is rejected by the Gateway", "HTTP 401", f"HTTP {resp['status']}", "PASS" if resp["status"] == 401 else "FAIL")
    status, text = signed_post(url, runtime, region, {"token": short, "run_id": "run-1"})
    if status == 200:
        ok = allowed(out["GatewayUrl"], json.loads(text)["access_token"], f"SpikeTargetA___{TOOL}")
        record("9", "refresh after expiry yields a working token", "allowed", "allowed" if ok else "denied", "PASS" if ok else "FAIL")
    else:
        record("9", "refresh after expiry yields a working token", "HTTP 200", f"HTTP {status}", "FAIL")


# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="S2 tool-scope spike test (see module docstring).")
    parser.add_argument("--stack", default="ToolScopeSpikeStack")
    parser.add_argument("--region", default=None, help="default: AWS_REGION / profile Region")
    parser.add_argument("--out", default=None, help="results JSON path (default: s2-results-<mode>.json)")
    parser.add_argument("--wait-expiry", action="store_true", help="wait ~5 min for a token to expire (task 9)")
    parser.add_argument("--skip-logs", action="store_true", help="skip the Gateway log check")
    parser.add_argument("--log-wait", type=int, default=60, help="seconds to wait for log delivery")
    args = parser.parse_args()

    region = args.region or boto3.Session().region_name
    if not region:
        print("No Region: pass --region or set AWS_REGION.", file=sys.stderr)
        return 2
    out = stack_outputs(region, args.stack)
    mode = out["PolicyMode"]
    caller = boto3.client("sts", region_name=region).get_caller_identity()["Arn"]
    print(f"Stack {args.stack} in {region}; policy mode {mode}; caller {caller}")
    started_ms = int(time.time() * 1000)

    runner = assume(out["RunnerRoleArn"], "s2-runner", region)
    runtime = assume(out["RuntimeRoleArn"], "s2-runtime", region)
    secret = check_secret_boundary(out, region, runner, runtime)
    tokens = check_tokens(out, secret)
    url = out["GatewayUrl"]
    check_handshake(url, tokens)
    check_matrix(url, tokens, mode)
    check_deny_shape(url, tokens, mode)
    check_refresh(out, region, runtime, secret, mode, args.wait_expiry)
    if not args.skip_logs:
        check_logs(out, region, started_ms, args.log_wait, mode)

    print()
    print_table()
    failed = [r for r in RESULTS if r["status"] == "FAIL"]
    print(f"\n{len(RESULTS)} checks: {sum(r['status'] == 'PASS' for r in RESULTS)} PASS, {len(failed)} FAIL, "
          f"{sum(r['status'] == 'WARN' for r in RESULTS)} WARN, {sum(r['status'] == 'INFO' for r in RESULTS)} INFO")

    path = args.out or f"s2-results-{mode.lower()}.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(
            {"timestamp": datetime.now(timezone.utc).isoformat(), "stack": args.stack, "region": region, "mode": mode,
             "caller": caller, "results": RESULTS, "raw": RAW},
            fh, indent=2, default=str,
        )
    print(f"Results written to {path}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
