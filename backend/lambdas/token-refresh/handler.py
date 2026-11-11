"""Token Refresh Lambda: POST /internal/token-refresh (B67, api.md, auth-and-tool-scope.md section 5).

A running agent calls this (AWS_IAM auth) before its 1-hour tool token expires. The Lambda checks the run is
still active, then mints a new Router client-credentials token with the run's scopes and the same user
sub / session id (passed as aws_client_metadata to the pre-token-generation trigger). The caller never sees
the Router M2M client secret.

Scopes come from the server-side run record, never from the caller (S2 finding: the stub read them from the
caller's token, which the real endpoint must not do).

Real-time runs prove the user with a verified user access token and an Active-run lease. Background jobs
(F16) can outlive that one-hour token, so a job refresh is authenticated by the allowlisted runtime role plus
an active Job ID Store record bound to the same user, session and job; an optional user token must match.
Callers outside ALLOWED_CALLER_ROLE_ARNS always get 403.
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
from functools import lru_cache
from typing import Any, Callable

import boto3
from shared.persistence.active_runs import ActiveRunRepository
from shared.persistence.jobs import RUNNING, JobRepository, valid_job_id
from shared.runtime.identity import CognitoAccessTokenVerifier, UnauthenticatedError, verify_gateway_token

from http_errors import ApiError, api_handler, get_logger, invalid_request, json_response, not_found, parse_json_body

logger = get_logger(__name__)

_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_USER_ID_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,128}$")
# arn:aws:sts::<account>:assumed-role/<role-name>/<session>  (what API Gateway reports for a role caller)
_ASSUMED_ROLE_ARN = re.compile(r"^arn:aws[a-z-]*:sts::(\d{12}):assumed-role/([\w+=,.@-]+)/[\w+=,.@-]+$")
# arn:aws:iam::<account>:role/<optional/path/><role-name>
_ROLE_ARN = re.compile(r"^arn:aws[a-z-]*:iam::(\d{12}):role/(?:[\w+=,.@/-]*/)?([\w+=,.@-]+)$")


@dataclass(frozen=True)
class ActiveRun:
    user_sub: str
    session_id: str
    scopes: tuple[str, ...]


def lookup_active_run(user_id: str, session_id: str, run_id: str, job_id: str | None) -> ActiveRun | None:
    """The active run for this session/job, or None.

    Real-time runs need an Active-run lease with this run id. Jobs need a running,
    uncancelled Job ID Store record for this user and session; scopes come from it.
    """
    if job_id:
        return lookup_active_job(user_id, session_id, job_id)
    if not os.environ.get("ACTIVE_RUNS_TABLE") or not os.environ.get("METADATA_TABLE"):
        return None
    dynamo = boto3.resource("dynamodb")
    owner = dynamo.Table(os.environ["METADATA_TABLE"]).get_item(
        Key={"user_id": user_id, "session_id": session_id}, ConsistentRead=True).get("Item")
    if not owner:
        return None
    lease = ActiveRunRepository(dynamo.Table(os.environ["ACTIVE_RUNS_TABLE"])).find(user_id, session_id)
    if not lease or lease.run_id != run_id:
        return None
    return ActiveRun(user_sub=user_id, session_id=session_id, scopes=lease.scopes)


def lookup_active_job(user_id: str, session_id: str, job_id: str) -> ActiveRun | None:
    if not os.environ.get("JOB_TABLE"):
        return None
    job = JobRepository(os.environ["JOB_TABLE"]).get(user_id, job_id)
    if (not job or job.get("status") != RUNNING or job.get("cancel_requested")
            or job.get("session_id") != session_id or not job.get("scopes")):
        return None
    return ActiveRun(user_sub=user_id, session_id=session_id, scopes=tuple(job["scopes"]))


RunLookup = Callable[[str, str, str, "str | None"], "ActiveRun | None"]
_run_lookup: RunLookup = lookup_active_run
_client_secret: str | None = None


def verify_user(token: str) -> str:
    pool, client = os.environ.get("USER_POOL_ID", ""), os.environ.get("SPA_CLIENT_ID", "")
    if not pool or not client:
        raise UnauthenticatedError("verification not configured")
    return str(CognitoAccessTokenVerifier(pool, os.environ["AWS_REGION"], [client]).verify(token)["sub"])


_user_verifier: Callable[[str], str] = verify_user


@lru_cache(maxsize=1)
def gateway_verifier() -> CognitoAccessTokenVerifier:
    return CognitoAccessTokenVerifier(os.environ["USER_POOL_ID"], os.environ["AWS_REGION"], [os.environ["ROUTER_CLIENT_ID"]], require_subject=False)


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


def _verified_user(token: Any) -> str:
    if not isinstance(token, str) or not token or len(token) > 8192:
        raise ApiError(401, "unauthorized", "Authentication is required.")
    try:
        return _user_verifier(token)
    except UnauthenticatedError:
        raise ApiError(401, "unauthorized", "Authentication is required.") from None


@api_handler(logger)
def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    if (event.get("httpMethod"), event.get("resource")) != ("POST", "/internal/token-refresh"):
        raise ApiError(404, "not_found", "The route was not found.")
    _check_caller(event)

    body = parse_json_body(event)
    unknown = sorted(set(body) - {"session_id", "run_id", "user_token", "job_id", "user_id"})
    if unknown:
        raise invalid_request(f"Unknown field: {unknown[0]}.")
    session_id = body.get("session_id")
    if not isinstance(session_id, str) or not _SESSION_ID_PATTERN.match(session_id):
        raise invalid_request("The session_id field is not valid.")
    job_id = body.get("job_id")
    if job_id is not None and not valid_job_id(job_id):
        raise invalid_request("The job_id field is not valid.")

    run_id, token = body.get("run_id"), body.get("user_token")
    if not isinstance(run_id, str) or not _SESSION_ID_PATTERN.match(run_id):
        raise invalid_request("The run_id field is not valid.")
    if job_id is not None:
        # A job outlives the user's one-hour token; the job record bound to this user authorizes the refresh.
        user_id = body.get("user_id")
        if not isinstance(user_id, str) or not _USER_ID_PATTERN.match(user_id) or run_id != job_id:
            raise invalid_request("The job refresh request is not valid.")
        if token is not None and _verified_user(token) != user_id:
            raise ApiError(401, "unauthorized", "Authentication is required.")
    else:
        if body.get("user_id") is not None:
            raise invalid_request("Unknown field: user_id.")
        user_id = _verified_user(token)
    run = _run_lookup(user_id, session_id, run_id, job_id)
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

    try:
        access_token = token.get("access_token") if isinstance(token, dict) else None
        if not isinstance(access_token, str) or not access_token:
            raise ValueError("invalid token grant")
        granted = verify_gateway_token(access_token, gateway_verifier(), run.scopes, user_id, session_id)
    except (ValueError, UnauthenticatedError):
        logger.error(json.dumps({"event": "token_refresh_invalid_grant"}))
        raise ApiError(503, "service_unavailable", "The token service is unavailable.", retriable=True) from None

    logger.info(json.dumps({"event": "token_refreshed", "scopes": granted}))
    return json_response(
        200,
        {"access_token": token["access_token"], "expires_in": token.get("expires_in"), "scopes": granted},
        event,
    )
