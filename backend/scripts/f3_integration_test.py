#!/usr/bin/env python3
"""F3 deployed integration check (phase file: .agents/phases/F03-api-session-store.md).

Runs the F3 acceptance criteria against the deployed REST API with real Cognito tokens:
  - no/garbage token -> 401 `unauthorized` envelope on every F3 route
  - POST /sessions -> GET /sessions -> GET /sessions/{id} -> PATCH -> GET /sessions/{id}/history
  - malformed request -> 400 `invalid_request`
  - cross-user isolation: user B cannot list, read, patch, or read history of user A's session
  - burst over the 20 rps stage throttle -> 429 `rate_limited` envelope (skip with --no-throttle)

Login uses the Cognito managed login page with the authorization-code + PKCE flow and the
already-allowed `http://localhost:5173/auth/callback` redirect. Tokens stay in this process's
memory only and are never printed. Each login first signs out of the Cognito session cookie so
the second login can be a different account.

Stdlib only. AWS CLI (with AWS_PROFILE or --profile) is used to read stack outputs and to clean
up the test sessions this script creates (F3 has no DELETE route; that is F19).

Usage (from repo root):
  python3 backend/scripts/f3_integration_test.py --profile ciccei
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import hashlib
import http.server
import json
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from typing import Any

CALLBACK_PORT = 5173
REDIRECT_URI = f"http://localhost:{CALLBACK_PORT}/auth/callback"
LOGOUT_URI = f"http://localhost:{CALLBACK_PORT}/"
LOGIN_TIMEOUT_S = 300

RESULTS: list[tuple[str, bool, str]] = []


# --------------------------------------------------------------------------- helpers


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not ok else ""))
    return ok


def aws(profile: str, region: str, *args: str) -> Any:
    cmd = ["aws", *args, "--region", region, "--output", "json"]
    if profile:
        cmd += ["--profile", profile]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"aws {' '.join(args[:2])} failed: {out.stderr.strip()}")
    return json.loads(out.stdout) if out.stdout.strip() else None


def stack_config(profile: str, region: str, stack: str) -> dict[str, str]:
    data = aws(profile, region, "cloudformation", "describe-stacks", "--stack-name", stack)
    outputs = {o["OutputKey"]: o["OutputValue"] for o in data["Stacks"][0].get("Outputs", [])}
    resources = aws(
        profile, region, "cloudformation", "list-stack-resources", "--stack-name", stack
    )
    table = next(
        r["PhysicalResourceId"]
        for r in resources["StackResourceSummaries"]
        if r["ResourceType"] == "AWS::DynamoDB::Table"
        and r["LogicalResourceId"].startswith("ChatMetadataStore")
    )
    return {
        "api": outputs["ApiBaseUrl"].rstrip("/"),
        "client_id": outputs["UserPoolClientId"],
        "domain": outputs["CognitoDomain"].rstrip("/"),
        "metadata_table": table,
    }


def jwt_claims(token: str) -> dict[str, Any]:
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def request(method: str, url: str, token: str | None = None, body: Any = None) -> tuple[int, Any]:
    headers = {"Accept": "application/json"}
    data = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    try:
        return status, json.loads(raw) if raw else None
    except ValueError:
        return status, raw.decode(errors="replace")


def is_envelope(body: Any, code: str) -> bool:
    err = body.get("error") if isinstance(body, dict) else None
    return (
        isinstance(err, dict)
        and err.get("code") == code
        and isinstance(err.get("message"), str)
        and isinstance(err.get("request_id"), str)
        and isinstance(err.get("retriable"), bool)
    )


# --------------------------------------------------------------------------- login (PKCE)


def login(cfg: dict[str, str], label: str) -> str:
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    state = secrets.token_urlsafe(24)
    authorize_url = f"{cfg['domain']}/oauth2/authorize?" + urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": cfg["client_id"],
            "redirect_uri": REDIRECT_URI,
            "scope": "openid email profile",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
    )
    # Sign out of any existing Cognito session first; Cognito then redirects to LOGOUT_URI ("/"),
    # which this server forwards to the authorize URL so the user always sees a fresh login form.
    logout_url = f"{cfg['domain']}/logout?" + urllib.parse.urlencode(
        {"client_id": cfg["client_id"], "logout_uri": LOGOUT_URI}
    )
    captured: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        # Browsers open idle "preconnect" sockets; without a socket timeout a handler thread
        # would block on them forever. Threaded server + timeout keeps real requests flowing.
        timeout = 5

        def log_message(self, *_args: Any) -> None:  # keep the console clean
            pass

        def _html(self, status: int, text: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                f"<html><body style='font-family:sans-serif'><p>{text}</p></body></html>".encode()
            )

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/":
                self.send_response(302)
                self.send_header("Location", authorize_url)
                self.end_headers()
                return
            if parsed.path == "/auth/callback":
                qs = urllib.parse.parse_qs(parsed.query)
                if qs.get("state", [""])[0] != state:
                    # A login page left open from an earlier attempt; ignore it and keep waiting
                    # for the current one instead of aborting the whole run.
                    self._html(
                        400,
                        "This tab is from an earlier attempt. Close it and use the newest login tab.",
                    )
                elif "error" in qs:
                    self._html(400, f"Login failed: {qs['error'][0]}")
                    captured["error"] = qs["error"][0]
                else:
                    captured["code"] = qs.get("code", [""])[0]
                    self._html(
                        200,
                        f"Logged in as {label}. You can close this tab and return to the terminal.",
                    )
                return
            self._html(404, "Not found.")

    try:
        server = http.server.ThreadingHTTPServer(("localhost", CALLBACK_PORT), Handler)
    except OSError:
        sys.exit(f"Port {CALLBACK_PORT} is busy. Stop any `npm run dev` (Vite) server and re-run.")
    server.daemon_threads = True
    threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True
    ).start()
    print(f"\n>>> Log in as {label} in the browser window that just opened.")
    print(f"    (If no window opened, visit: {logout_url})")
    webbrowser.open(logout_url)
    deadline = time.time() + LOGIN_TIMEOUT_S
    while "code" not in captured and "error" not in captured and time.time() < deadline:
        time.sleep(0.2)
    server.shutdown()
    server.server_close()
    if "code" not in captured:
        sys.exit(f"Login for {label} did not complete: {captured.get('error', 'timed out')}")

    form = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "client_id": cfg["client_id"],
            "code": captured["code"],
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
        }
    ).encode()
    req = urllib.request.Request(
        f"{cfg['domain']}/oauth2/token",
        data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        tokens = json.loads(resp.read())
    return tokens["access_token"]


# --------------------------------------------------------------------------- test sections


def test_unauthenticated(api: str) -> None:
    print("\n== 1. No token / garbage token is rejected with the 401 envelope ==")
    routes = [
        ("POST", "/sessions", {"title": "x", "mode": "science"}),
        ("GET", "/sessions", None),
        ("GET", "/sessions/does-not-exist", None),
        ("PATCH", "/sessions/does-not-exist", {"title": "x"}),
        ("GET", "/sessions/does-not-exist/history", None),
    ]
    for method, path, body in routes:
        status, resp = request(method, api + path, None, body)
        check(
            f"{method} {path} without token -> 401 unauthorized",
            status == 401 and is_envelope(resp, "unauthorized"),
            f"{status} {resp}",
        )
    status, resp = request("GET", api + "/sessions", "not.a.jwt")
    check(
        "GET /sessions with garbage token -> 401 unauthorized",
        status == 401 and is_envelope(resp, "unauthorized"),
        f"{status} {resp}",
    )


def test_user_a_flow(api: str, token: str) -> str | None:
    print("\n== 2. User A: create -> list -> get -> patch -> history ==")
    status, created = request(
        "POST", api + "/sessions", token, {"title": "F3 integration test", "mode": "science"}
    )
    ok = status == 201 and isinstance(created, dict) and isinstance(created.get("session_id"), str)
    check("POST /sessions -> 201 with session_id", ok, f"{status} {created}")
    if not ok:
        return None
    sid = created["session_id"]
    check(
        "POST response has title, mode=science, status, created_at",
        created.get("title") == "F3 integration test"
        and created.get("mode") == "science"
        and "status" in created
        and "created_at" in created,
        str(created),
    )

    status, page = request("GET", api + "/sessions", token)
    ids = (
        [s.get("session_id") for s in (page or {}).get("items", [])]
        if isinstance(page, dict)
        else []
    )
    check(
        "GET /sessions -> 200 and lists the new session",
        status == 200 and sid in ids,
        f"{status} ids={ids}",
    )
    check(
        "GET /sessions has the pagination envelope (items, next_cursor, has_more)",
        isinstance(page, dict) and {"items", "next_cursor", "has_more"} <= set(page),
        str(page)[:200],
    )
    check(
        "GET /sessions never exposes user_id",
        all("user_id" not in s for s in (page or {}).get("items", [])),
    )

    status, one = request("GET", f"{api}/sessions/{sid}", token)
    check(
        "GET /sessions/{id} -> 200 with matching title",
        status == 200 and (one or {}).get("title") == "F3 integration test",
        f"{status} {one}",
    )

    status, patched = request(
        "PATCH", f"{api}/sessions/{sid}", token, {"title": "F3 renamed", "mode": "business"}
    )
    check(
        "PATCH /sessions/{id} -> 200 with new title/mode",
        status == 200
        and (patched or {}).get("title") == "F3 renamed"
        and (patched or {}).get("mode") == "business",
        f"{status} {patched}",
    )
    status, one = request("GET", f"{api}/sessions/{sid}", token)
    check(
        "Subsequent GET reflects the PATCH",
        status == 200
        and (one or {}).get("title") == "F3 renamed"
        and (one or {}).get("mode") == "business",
        f"{status} {one}",
    )

    status, hist = request("GET", f"{api}/sessions/{sid}/history", token)
    check(
        "GET /sessions/{id}/history -> 200 empty envelope",
        status == 200 and hist == {"items": [], "next_cursor": None, "has_more": False},
        f"{status} {hist}",
    )

    print("\n== 3. Malformed requests -> 400 invalid_request ==")
    for label, method, path, body in [
        ("bad mode", "POST", "/sessions", {"title": "x", "mode": "art"}),
        ("missing mode", "POST", "/sessions", {"title": "x"}),
        (
            "unknown field",
            "POST",
            "/sessions",
            {"title": "x", "mode": "science", "user_id": "someone-else"},
        ),
        ("empty PATCH", "PATCH", f"/sessions/{sid}", {}),
        ("limit=0", "GET", "/sessions?limit=0", None),
        ("limit=101", "GET", "/sessions?limit=101", None),
        ("bad cursor", "GET", "/sessions?cursor=not-a-cursor", None),
    ]:
        status, resp = request(method, api + path, token, body)
        check(
            f"{label} -> 400 invalid_request",
            status == 400 and is_envelope(resp, "invalid_request"),
            f"{status} {resp}",
        )
    return sid


def test_isolation(api: str, token_b: str, sid_a: str) -> str | None:
    print("\n== 4. User B cannot see or touch user A's session ==")
    status, page = request("GET", api + "/sessions", token_b)
    ids = (
        [s.get("session_id") for s in (page or {}).get("items", [])]
        if isinstance(page, dict)
        else []
    )
    check(
        "User B GET /sessions does not list A's session",
        status == 200 and sid_a not in ids,
        f"{status} ids={ids}",
    )
    for method, path, body in [
        ("GET", f"/sessions/{sid_a}", None),
        ("PATCH", f"/sessions/{sid_a}", {"title": "hijacked"}),
        ("GET", f"/sessions/{sid_a}/history", None),
    ]:
        status, resp = request(method, api + path, token_b, body)
        check(
            f"User B {method} {path.replace(sid_a, '{A}')} -> 404 not_found",
            status == 404 and is_envelope(resp, "not_found"),
            f"{status} {resp}",
        )

    # B's own session works, and A's title was not changed by B's PATCH attempt
    status, created = request(
        "POST", api + "/sessions", token_b, {"title": "F3 user B", "mode": "business"}
    )
    check("User B can create its own session", status == 201, f"{status} {created}")
    return (created or {}).get("session_id") if status == 201 else None


def test_throttle(api: str, token: str, burst: int) -> None:
    print(f"\n== 5. Burst of {burst} concurrent requests -> 429 rate_limited envelope ==")

    def one(_: int) -> tuple[int, Any]:
        return request("GET", api + "/sessions?limit=1", token)

    # ADR: 100 workers, default 300 requests | Rationale: a measured 60-thread burst only reached ~7 rps
    # (TLS setup spreads it over seconds) and never exceeded the 20 rps token bucket; 300/100 reached ~250 rps
    started = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(burst, 100)) as pool:
        results = list(pool.map(one, range(burst)))
    elapsed = time.time() - started
    print(f"    {burst} requests in {elapsed:.1f}s (~{burst / elapsed:.0f} rps)")
    statuses = [s for s, _ in results]
    throttled = [b for s, b in results if s == 429]
    counts = {s: statuses.count(s) for s in sorted(set(statuses))}
    check("At least one request throttled with 429", bool(throttled), f"status counts {counts}")
    if throttled:
        check(
            "429 body is the rate_limited envelope with retriable=true",
            is_envelope(throttled[0], "rate_limited")
            and throttled[0]["error"]["retriable"] is True,
            str(throttled[0]),
        )
    print(f"    status counts: {counts}")


def cleanup(profile: str, region: str, table: str, items: list[tuple[str, str]]) -> None:
    print("\n== Cleanup: deleting the test sessions this run created ==")
    for user_sub, sid in items:
        key = json.dumps({"user_id": {"S": user_sub}, "session_id": {"S": sid}})
        try:
            aws(profile, region, "dynamodb", "delete-item", "--table-name", table, "--key", key)
            print(f"  deleted session {sid}")
        except RuntimeError as exc:
            print(f"  could not delete session {sid}: {exc}")


# --------------------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--profile", default="", help="AWS CLI profile (default: AWS_PROFILE / default chain)"
    )
    parser.add_argument("--region", default="us-east-2")
    parser.add_argument("--stack", default="BackendStack")
    parser.add_argument("--no-throttle", action="store_true", help="skip the 429 burst test")
    parser.add_argument("--burst", type=int, default=300)
    parser.add_argument(
        "--only-throttle", action="store_true", help="one login, run only the 429 burst test"
    )
    parser.add_argument(
        "--keep", action="store_true", help="do not delete the test sessions afterwards"
    )
    args = parser.parse_args()

    cfg = stack_config(args.profile, args.region, args.stack)
    api = cfg["api"]
    print(f"API: {api}")

    if args.only_throttle:
        test_throttle(api, login(cfg, "USER A"), args.burst)
        failed = [n for n, ok, _ in RESULTS if not ok]
        print(f"\n==== F3 throttle check: {len(RESULTS) - len(failed)}/{len(RESULTS)} passed ====")
        return 0 if not failed else 1

    test_unauthenticated(api)

    token_a = login(cfg, "USER A")
    sub_a = jwt_claims(token_a).get("sub", "")
    check(
        "User A token is an access token with openid scope",
        jwt_claims(token_a).get("token_use") == "access"
        and "openid" in jwt_claims(token_a).get("scope", ""),
    )
    created: list[tuple[str, str]] = []
    sid_a = test_user_a_flow(api, token_a)
    if sid_a:
        created.append((sub_a, sid_a))

    token_b = login(cfg, "USER B (a DIFFERENT account from user A)")
    sub_b = jwt_claims(token_b).get("sub", "")
    if not check("User B is a different Cognito user than user A", sub_a != sub_b):
        print("    Both logins were the same account; isolation results below are not meaningful.")
    if sid_a:
        sid_b = test_isolation(api, token_b, sid_a)
        if sid_b:
            created.append((sub_b, sid_b))
        status, one = request("GET", f"{api}/sessions/{sid_a}", token_a)
        check(
            "User A's session is unchanged after B's PATCH attempt",
            (one or {}).get("title") == "F3 renamed",
            f"{status} {one}",
        )

    if not args.no_throttle:
        test_throttle(api, token_a, args.burst)

    if created and not args.keep:
        cleanup(args.profile, args.region, cfg["metadata_table"], created)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [(n, d) for n, ok, d in RESULTS if not ok]
    print(f"\n==== F3 integration check: {passed}/{len(RESULTS)} passed ====")
    for name, detail in failed:
        print(f"  FAIL: {name}  {detail}")
    print("Not covered: expired-token 401 (needs a token past its 1-hour lifetime).")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
