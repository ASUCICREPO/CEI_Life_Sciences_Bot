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
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from functools import lru_cache
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError

# ADR: make sibling classifier.py importable regardless of caller | Rationale: AWS Lambda already puts this
# directory on sys.path, but the pytest harness loads this file via importlib (spec_from_file_location),
# which does not; this insertion is a no-op in Lambda and required under pytest.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from classifier import AGENTS, GuardrailIntervened, classify  # noqa: E402

from shared.config import D3_BLOCKED_MODEL_PATTERNS
from shared.persistence.active_runs import ActiveRunRepository, ConcurrencyLimitError
from shared.persistence.ownership import is_session_uuid
from shared.runtime.identity import CognitoAccessTokenVerifier, UnauthenticatedError, verify_gateway_token
from shared.runtime.model_policy import is_blocked

_RUN_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# F9/B15/live-drafting: "live-research" and "live-drafting" are today's deterministic dashboard task lookups
# (F5); absent means free text, routed by the classifier. handler.py's env-var naming for each routed agent's
# runtime URL.
_DASHBOARD_TASK_AGENTS = {"live-research": "research", "live-drafting": "drafting"}
_RUNTIME_URL_ENV = {
    "research": "RESEARCH_RUNTIME_URL",
    "drafting": "DRAFTING_RUNTIME_URL",
    "analysis": "ANALYSIS_RUNTIME_URL",
    "pipeline_manager": "PIPELINE_RUNTIME_URL",
}
DEFAULT_CONFIDENCE_THRESHOLD = 0.7
# F9/B80: the ChoicePicker turn ends without a run; the pending choice outlives it in the Active-Run table
# (same table as the realtime/background leases -- not a new table) until UiActions consumes or it expires.
ROUTE_CHOICE_TTL_SECONDS = 600


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


@lru_cache(maxsize=1)
def runs_table() -> Any:
    # Separate from tables()'s ActiveRunRepository: route_choice records are not lease records, but they
    # live in the same table (F9 avoids a new table per the phase note).
    return boto3.resource("dynamodb").Table(os.environ["ACTIVE_RUNS_TABLE"])


@lru_cache(maxsize=1)
def bedrock_client() -> Any:
    return boto3.client("bedrock-runtime")


def store_pending_choice(user_id: str, session_id: str, *, message: str, mode: str, surface_id: str) -> None:
    runs_table().update_item(
        Key={"user_id": user_id, "record": f"route_choice#{session_id}"},
        UpdateExpression="SET #m = :message, #mode = :mode, #sid = :surface_id, #exp = :expires_at",
        ExpressionAttributeNames={"#m": "message", "#mode": "mode", "#sid": "surface_id", "#exp": "expires_at"},
        ExpressionAttributeValues={
            ":message": message, ":mode": mode, ":surface_id": surface_id,
            ":expires_at": int(time.time()) + ROUTE_CHOICE_TTL_SECONDS,
        },
    )


def _model_blocklist() -> tuple[str, ...]:
    # ADR: read os.environ directly, not shared.config's AgentConfig singleton | Rationale: that singleton is
    # constructed once at first import and would go stale across the env-var changes this Lambda's own tests make
    # per test case; is_blocked/D3_BLOCKED_MODEL_PATTERNS are pure and have no such staleness problem.
    extra = tuple(p.strip() for p in os.environ.get("CEI_BLOCKED_MODELS", "").split(",") if p.strip())
    return D3_BLOCKED_MODEL_PATTERNS + extra


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
        task_type = request.get("task_type")
        message = request.get("message")
        if (request.get("mode") not in ("science", "business") or not isinstance(message, str)
                or not message.strip() or len(message) > 16000):
            return {"status": 400, "code": "invalid_request", "message": "Invalid request."}
        mode = request["mode"]
        # F5/live-drafting: today's deterministic dashboard task types, unchanged. F9: absent means free text
        # -- classified below. Anything else is not a route this Lambda knows about.
        if task_type is not None and task_type not in _DASHBOARD_TASK_AGENTS:
            return {"status": 400, "code": "invalid_request", "message": "Invalid request."}
        # Fail before the Runner opens SSE. The runtime repeats the check because
        # settings can change after this owner-scoped, strongly consistent read.
        # B18: any non-blocklisted model is allowed, not only CEI_DEFAULT_MODEL_ID; real availability
        # (server discovery) was already checked once at write time (models-settings-handler,
        # session-metadata-handler — shared/runtime/model_discovery.py), so this is a blocklist-only recheck.
        # Applies to every route, not only live-research: whichever specialist runs reads this same session.
        default_model = os.environ.get("CEI_DEFAULT_MODEL_ID", "").strip()
        if not default_model:
            raise ValueError("model configuration missing")
        candidate = session.get("model_id") or default_model
        if is_blocked(candidate, _model_blocklist()):
            return {"status": 400, "code": "model_unavailable", "message": "This chat's model is unavailable."}
        scopes = tuple(s for s in os.environ["TOOL_SCOPES"].split(",") if s)
        if not scopes or any(not s.startswith("cei-tools/") for s in scopes):
            raise ValueError("invalid scope configuration")

        if task_type in _DASHBOARD_TASK_AGENTS:
            agent = _DASHBOARD_TASK_AGENTS[task_type]
        else:
            # F9/B15: free text. `route_agent` is never accepted from a browser request -- only UiActions
            # sends it, after it has itself verified ownership and the stored pending choice (CLASSIFIER.md
            # step 4); it skips reclassification and routes the user's original message to that agent.
            route_agent = request.get("route_agent")
            if route_agent is not None:
                if route_agent not in AGENTS:
                    return {"status": 400, "code": "invalid_request", "message": "Invalid request."}
                agent = route_agent
            else:
                stage = "classify"
                try:
                    classification = classify(
                        message, client=bedrock_client(), model_id=os.environ.get("CEI_ROUTER_MODEL_ID", ""),
                        guardrail_id=os.environ.get("CEI_GUARDRAIL_ID", ""),
                        guardrail_version=os.environ.get("CEI_GUARDRAIL_VERSION", ""),
                    )
                except GuardrailIntervened:
                    return {"status": 400, "code": "invalid_request", "message": "This message cannot be processed."}
                # Any other ClassificationError (bad config, model/guardrail failure, invalid output) bubbles
                # to the generic `except Exception` below: 503, fail closed, never routes or mints a token.
                try:
                    threshold = float(os.environ.get("CEI_ROUTER_CONFIDENCE_THRESHOLD", ""))
                except ValueError:
                    threshold = DEFAULT_CONFIDENCE_THRESHOLD
                if classification.needs_choice(threshold):
                    surface_id = f"router-choice-{run_id}"
                    stage = "persist_pending_choice"
                    store_pending_choice(user_id, session_id, message=message, mode=mode, surface_id=surface_id)
                    return {"status": 200, "run_id": run_id, "session_id": session_id, "user_id": user_id,
                            "choice_required": True, "surface_id": surface_id}
                agent = classification.agent

        # Never report a destination available until its runtime is actually wired (checked before any
        # lease/token is spent, same as a missing-configuration failure elsewhere in this handler).
        runtime_url = os.environ.get(_RUNTIME_URL_ENV[agent], "")
        if not runtime_url:
            raise ValueError("runtime configuration missing")

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
                "gateway_token": token, "runtime_url": runtime_url, "agent": agent}
    except UnauthenticatedError:
        log("unauthenticated")
        return {"status": 401, "code": "unauthorized", "message": "Authentication is required."}
    except ConcurrencyLimitError:
        log("concurrency_limit")
        return {"status": 429, "code": "concurrency_limit", "message": "Too many active runs."}
    except Exception as exc:
        log("failed", exc)
        return {"status": 503, "code": "service_unavailable", "message": "The chat service is temporarily unavailable."}
