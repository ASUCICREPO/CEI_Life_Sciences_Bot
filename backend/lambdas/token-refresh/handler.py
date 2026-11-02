"""Token Refresh Lambda: POST /internal/token-refresh (B67, api.md, auth-and-tool-scope.md section 5).

A running agent calls this (AWS_IAM auth) before its 1-hour tool token expires. The Lambda checks the run is
still active, then mints a new Router client-credentials token with the run's scopes and the same user
sub / session id (passed as aws_client_metadata to the pre-token-generation trigger). The caller never sees
the Router M2M client secret.

Scopes come from the server-side run record, never from the caller (S2 finding: the stub read them from the
caller's token, which the real endpoint must not do).

F4 state, fail closed:
- No active-run record exists yet (F5 builds real-time runs, F16 the Job ID Store), so `lookup_active_run`
  returns None and every request is refused with 404.
- No agent runtime role exists yet (F5), so ALLOWED_CALLER_ROLE_ARNS is empty and every caller gets 403.
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

import boto3

from http_errors import ApiError, api_handler, get_logger, invalid_request, json_response, not_found, parse_json_body

logger = get_logger(__name__)

_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# arn:aws:sts::<account>:assumed-role/<role-name>/<session>  (what API Gateway reports for a role caller)
_ASSUMED_ROLE_ARN = re.compile(r"^arn:aws[a-z-]*:sts::(\d{12}):assumed-role/([\w+=,.@-]+)/[\w+=,.@-]+$")
# arn:aws:iam::<account>:role/<optional/path/><role-name>
_ROLE_ARN = re.compile(r"^arn:aws[a-z-]*:iam::(\d{12}):role/(?:[\w+=,.@/-]*/)?([\w+=,.@-]+)$")


@dataclass(frozen=True)
class ActiveRun:
    user_sub: str
    session_id: str
    scopes: tuple[str, ...]


def lookup_active_run(session_id: str, job_id: str | None) -> ActiveRun | None:
    """The active run for this session/job, or None.

    ADR: always None in F4 | Rationale: B67 — the active-run record (real-time runs, F5) and the Job ID Store
    (background jobs, F16) do not exist yet; failing closed means no token can be refreshed for a run nobody
    can prove is active | Alternative: trusting the caller's session id rejected — any allowed role could then
    mint tokens for any session
    """
    return None


RunLookup = Callable[[str, "str | None"], "ActiveRun | None"]
_run_lookup: RunLookup = lookup_active_run
_client_secret: str | None = None


def _env_list(name: str) -> list[str]:
    return [v.strip() for v in os.environ.get(name, "").split(",") if v.strip()]


def _role_key(arn: str) -> tuple[str, str] | None:
    """(account, role name) for an IAM role ARN or an STS assumed-role session ARN."""
    for pattern in (_ASSUMED_ROLE_ARN, _ROLE_ARN):
        match = pattern.match(arn or "")
        if match:
            return match.group(1), match.group(2)
    return None


def _check_caller(event: dict[str, Any]) -> None:
    caller = ((event.get("requestContext") or {}).get("identity") or {}).get("userArn") or ""
    allowed = {key for key in (_role_key(a) for a in _env_list("ALLOWED_CALLER_ROLE_ARNS")) if key}
    key = _role_key(caller)
    if key is None or key not in allowed:
        logger.warning(json.dumps({"event": "token_refresh_denied", "reason": "caller_not_allowed"}))
        raise ApiError(403, "forbidden", "Access is denied.")


def _read_client_secret() -> str:
    global _client_secret
    if _client_secret is None:
        response = boto3.client("secretsmanager").get_secret_value(SecretId=os.environ["ROUTER_SECRET_ARN"])
        _client_secret = response["SecretString"]
    return _client_secret


def request_token(run: ActiveRun) -> dict[str, Any]:
    """Client-credentials token for the run's scopes, with the user sub / session id as client metadata."""
    client_id = os.environ["ROUTER_CLIENT_ID"]
    basic = base64.b64encode(f"{client_id}:{_read_client_secret()}".encode()).decode()
    form = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "scope": " ".join(run.scopes),
            "aws_client_metadata": json.dumps({"cei_user_sub": run.user_sub, "cei_session_id": run.session_id}),
        }
    ).encode()
    request = urllib.request.Request(
        os.environ["TOKEN_ENDPOINT"],
        data=form,
        headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as resp:  # noqa: S310 - fixed https Cognito endpoint
        return json.loads(resp.read())


_token_requester: Callable[[ActiveRun], dict[str, Any]] = request_token


@api_handler(logger)
def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    if (event.get("httpMethod"), event.get("resource")) != ("POST", "/internal/token-refresh"):
        raise ApiError(404, "not_found", "The route was not found.")
    _check_caller(event)

    body = parse_json_body(event)
    unknown = sorted(set(body) - {"session_id", "job_id"})
    if unknown:
        raise invalid_request(f"Unknown field: {unknown[0]}.")
    session_id = body.get("session_id")
    if not isinstance(session_id, str) or not _SESSION_ID_PATTERN.match(session_id):
        raise invalid_request("The session_id field is not valid.")
    job_id = body.get("job_id")
    if job_id is not None and (not isinstance(job_id, str) or not _JOB_ID_PATTERN.match(job_id)):
        raise invalid_request("The job_id field is not valid.")

    run = _run_lookup(session_id, job_id)
    if run is None or run.session_id != session_id:
        logger.info(json.dumps({"event": "token_refresh_refused", "reason": "run_not_active"}))
        raise not_found("The run is not active.")

    allowed_scopes = set(_env_list("ALLOWED_SCOPES"))
    if not run.scopes or not set(run.scopes) <= allowed_scopes:
        logger.error(json.dumps({"event": "token_refresh_refused", "reason": "run_scopes_not_allowed"}))
        raise ApiError(500, "internal_error", "An internal error occurred.")

    try:
        token = _token_requester(run)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        status = getattr(exc, "code", None)
        logger.error(json.dumps({"event": "token_endpoint_error", "status": status}))
        raise ApiError(503, "service_unavailable", "The token service is unavailable.", retriable=True) from None

    granted = sorted(str(token.get("scope", "")).split())
    if granted != sorted(run.scopes):
        # Cognito never widens a client-credentials scope set, but a narrower grant must not pass silently
        logger.error(json.dumps({"event": "token_refresh_scope_mismatch"}))
        raise ApiError(500, "internal_error", "An internal error occurred.")

    logger.info(json.dumps({"event": "token_refreshed", "scopes": granted}))
    return json_response(
        200,
        {"access_token": token["access_token"], "expires_in": token.get("expires_in"), "scopes": granted},
        event,
    )
