"""Internal Runner-only authorization and tool-token broker.

Runner invokes this Lambda using IAM. The bearer token is always reverified; no
user identity or scope is accepted from the Runner's request body.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from functools import lru_cache
from typing import Any

import boto3
from botocore.exceptions import ClientError

from shared.persistence.active_runs import ActiveRunRepository, ConcurrencyLimitError
from shared.persistence.ownership import is_session_uuid
from shared.runtime.identity import CognitoAccessTokenVerifier, UnauthenticatedError, verify_gateway_token

_RUN_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
logger = logging.getLogger()
logger.setLevel(logging.INFO)


@lru_cache(maxsize=1)
def verifier() -> CognitoAccessTokenVerifier:
    return CognitoAccessTokenVerifier(os.environ["USER_POOL_ID"], os.environ["AWS_REGION"], [os.environ["SPA_CLIENT_ID"]])


@lru_cache(maxsize=1)
def gateway_verifier() -> CognitoAccessTokenVerifier:
    return CognitoAccessTokenVerifier(os.environ["USER_POOL_ID"], os.environ["AWS_REGION"], [os.environ["ROUTER_CLIENT_ID"]], require_subject=False)


@lru_cache(maxsize=1)
def tables() -> tuple[Any, ActiveRunRepository]:
    dynamo = boto3.resource("dynamodb")
    return dynamo.Table(os.environ["METADATA_TABLE"]), ActiveRunRepository(dynamo.Table(os.environ["ACTIVE_RUNS_TABLE"]))


def issue_token(user_id: str, session_id: str, scopes: tuple[str, ...]) -> str:
    secret = boto3.client("secretsmanager").get_secret_value(SecretId=os.environ["ROUTER_SECRET_ARN"])["SecretString"]
    client_id = os.environ["ROUTER_CLIENT_ID"]
    authorization = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    form = urllib.parse.urlencode({"grant_type": "client_credentials", "scope": " ".join(scopes),
        "aws_client_metadata": json.dumps({"cei_user_sub": user_id, "cei_session_id": session_id})}).encode()
    request = urllib.request.Request(os.environ["TOKEN_ENDPOINT"], data=form, method="POST", headers={
        "Authorization": f"Basic {authorization}", "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(request, timeout=10) as response:  # fixed deployment-configured Cognito endpoint
        grant = json.load(response)
    token = grant.get("access_token") if isinstance(grant, dict) else None
    if not isinstance(token, str) or not token:
        raise ValueError("invalid token grant")
    verify_gateway_token(token, gateway_verifier(), scopes, user_id, session_id)
    return token


def handler(event: Any, _context: Any) -> dict[str, Any]:
    stage = "validate"
    def log(outcome: str, exc: Exception | None = None) -> None:
        # Never log exception messages, token values, request bodies, or user identifiers.
        record = {"event": "session_router", "stage": stage, "outcome": outcome,
                  "lambda_request_id": getattr(_context, "aws_request_id", "unknown")}
        rid = event.get("request_id") if isinstance(event, dict) else None
        if isinstance(rid, str) and _RUN_ID.fullmatch(rid):
            record["request_id"] = rid
        if exc is not None:
            record["error_type"] = type(exc).__name__
            if isinstance(exc, urllib.error.HTTPError):
                record["http_status"] = exc.code
            if isinstance(exc, ClientError):
                code = exc.response.get("Error", {}).get("Code")
                if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_]{1,80}", code):
                    record["aws_error_code"] = code
        logger.warning(json.dumps(record))
    try:
        if not isinstance(event, dict) or event.get("op") not in ("start", "finish"):
            return {"status": 400, "code": "invalid_request", "message": "Invalid request."}
        bearer = event.get("bearer_token")
        if not isinstance(bearer, str) or not bearer:
            raise UnauthenticatedError("missing token")
        stage = "verify_identity"
        user_id = verifier().verify(bearer)["sub"]
        request = event.get("request") if event["op"] == "start" else event
        if not isinstance(request, dict):
            return {"status": 400, "code": "invalid_request", "message": "Invalid request."}
        session_id, run_id = request.get("session_id"), request.get("run_id")
        if not is_session_uuid(session_id) or not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            return {"status": 400, "code": "invalid_request", "message": "Invalid request."}
        stage = "session_lookup"
        metadata, leases = tables()
        session = metadata.get_item(Key={"user_id": user_id, "session_id": session_id}, ConsistentRead=True).get("Item")
        if not session:
            return {"status": 404, "code": "not_found", "message": "Session not found."}
        stage = "lease_lookup"
        existing = leases.find(user_id, session_id)
        if event["op"] == "finish":
            if existing and existing.run_id == run_id:
                leases.release(existing)
            return {"status": 200}
        if (request.get("task_type") != "live-research" or request.get("mode") not in ("science", "business")
                or not isinstance(request.get("message"), str) or not request["message"].strip()
                or len(request["message"]) > 16000):
            return {"status": 400, "code": "invalid_request", "message": "Invalid Research request."}
        # Fail before the Runner opens SSE. The runtime repeats the check because
        # settings can change after this owner-scoped, strongly consistent read.
        default_model = os.environ.get("CEI_DEFAULT_MODEL_ID", "").strip()
        if not default_model:
            raise ValueError("model configuration missing")
        selected = session.get("model_id")
        if selected is not None and selected != default_model:
            return {"status": 400, "code": "model_unavailable", "message": "This chat's model is unavailable."}
        scopes = tuple(s for s in os.environ["TOOL_SCOPES"].split(",") if s)
        if not scopes or any(not s.startswith("cei-tools/") for s in scopes):
            raise ValueError("invalid scope configuration")
        acquired = False
        if existing:
            if existing.run_id != run_id or existing.scopes != scopes:
                raise ConcurrencyLimitError()
            lease = existing
        else:
            stage = "lease_acquire"
            lease = leases.acquire(user_id, session_id, run_id, scopes)
            acquired = True
        try:
            stage = "issue_token"
            token = issue_token(user_id, session_id, scopes)
        except Exception:
            if acquired:
                leases.release(lease)
            raise
        return {"status": 200, "run_id": run_id, "session_id": session_id, "user_id": user_id,
                "gateway_token": token, "runtime_url": os.environ["RESEARCH_RUNTIME_URL"]}
    except UnauthenticatedError:
        log("unauthenticated")
        return {"status": 401, "code": "unauthorized", "message": "Authentication is required."}
    except ConcurrencyLimitError:
        log("concurrency_limit")
        return {"status": 429, "code": "concurrency_limit", "message": "Too many active runs."}
    except Exception as exc:
        log("failed", exc)
        return {"status": 503, "code": "service_unavailable", "message": "Research is temporarily unavailable."}
