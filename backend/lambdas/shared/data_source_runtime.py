"""Shared runtime for the F4 data-source Gateway Lambda targets.

Each `backend/lambdas/data-source-<id>/handler.py` is one line: `handler = make_handler(<Tool>, ...)`.
The tool's `fetch()` holds the real upstream call (with retry); this module adds the Gateway-facing
parts: input validation, the optional user API key lookup (B20/B78), and one response shape.

Shipped to the source Lambdas in the data-source layer (top-level module `data_source_runtime`).
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Callable

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import ValidationError

from shared.tools.base import DataSourceResult, DataSourceTool

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Every source target exposes exactly one tool; the Gateway names it `<TargetName>___search`.
TOOL_SUFFIX = "___search"
SECRET_PREFIX_ENV = "CEI_API_KEY_SECRET_PREFIX"
DEFAULT_SECRET_PREFIX = "cei/user-keys"
# Cognito `sub` values are UUIDs; anything else is refused before it can shape a secret name.
_USER_ID_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,128}$")
_SOURCE_ID_PATTERN = re.compile(r"^[a-z0-9-]{1,64}$")

UserIdResolver = Callable[[dict[str, Any], Any], str | None]


def unresolved_user_id(_event: dict[str, Any], _context: Any) -> str | None:
    """Default resolver: no trusted user identity reaches a Lambda target yet, so no user key is used.

    ADR: return None (free path) until the user-identity mechanism is decided | Rationale: the spec
    (auth-and-tool-scope.md section 5) has the Lambda read `sub` from the caller's token, but the
    Gateway does not forward `Authorization` to targets and JWT passthrough is HTTP-target only
    (AgentCore docs, gateway-headers.html, read 2026-10-28). Reading a user id from the tool
    arguments is rejected: the agent controls those, so it could select another user's key.
    """
    return None


def tool_name_from_context(context: Any) -> str | None:
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    name = custom.get("bedrockAgentCoreToolName")
    return name if isinstance(name, str) else None


def _error_result(tool: DataSourceTool, code: str, message: str, **extra: Any) -> dict[str, Any]:
    result = DataSourceResult(
        source_type=tool.source_type,
        results=[],
        citations=[],
        query_metadata={"error": code, "message": message, **extra},
    )
    return result.model_dump(mode="json")


def _secret_string_to_key(secret_string: str) -> str | None:
    """Accept the raw key, or JSON `{"api_key": "..."}` (F18 owns the write format)."""
    value = secret_string.strip()
    if value.startswith("{"):
        try:
            parsed = json.loads(value)
        except ValueError:
            return None
        key = parsed.get("api_key") if isinstance(parsed, dict) else None
        return key.strip() if isinstance(key, str) and key.strip() else None
    return value or None


def lookup_user_api_key(secrets_client: Any, user_id: str, source_id: str, prefix: str) -> str | None:
    """The user's own key for this source, or None. Never raises: any failure falls back to free (B20)."""
    if not _USER_ID_PATTERN.match(user_id) or not _SOURCE_ID_PATTERN.match(source_id):
        logger.warning(json.dumps({"event": "api_key_lookup_skipped", "reason": "invalid_id"}))
        return None
    secret_id = f"{prefix}/{user_id}/{source_id}"
    try:
        response = secrets_client.get_secret_value(SecretId=secret_id)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code != "ResourceNotFoundException":
            # ADR: log the error code only | Rationale: the secret name embeds the user id (PII-adjacent)
            logger.warning(json.dumps({"event": "api_key_lookup_failed", "code": code}))
        return None
    except BotoCoreError as exc:
        logger.warning(json.dumps({"event": "api_key_lookup_failed", "code": type(exc).__name__}))
        return None
    secret_string = response.get("SecretString")
    return _secret_string_to_key(secret_string) if isinstance(secret_string, str) else None


def make_handler(
    tool_cls: type[DataSourceTool],
    *,
    source_id: str,
    api_key_source_id: str | None = None,
    user_id_resolver: UserIdResolver = unresolved_user_id,
    secrets_client_factory: Callable[[], Any] | None = None,
) -> Callable[[Any, Any], dict[str, Any]]:
    """Build the Lambda handler for one source.

    source_id: the target id (`pubmed`, `clinicaltrials`, ...), as in the `cei-tools/src.<id>` scope.
    api_key_source_id: the `{source_id}` segment of the user-key secret name, when it differs
      from source_id (PubMed uses an NCBI account key, so its secret is `.../ncbi`).
    """
    if not _SOURCE_ID_PATTERN.match(source_id):
        raise ValueError(f"invalid source_id {source_id!r}")
    tool = tool_cls()
    key_source_id = api_key_source_id or source_id
    secrets_client: Any = None

    def _secrets() -> Any:
        nonlocal secrets_client
        if secrets_client is None:
            secrets_client = secrets_client_factory() if secrets_client_factory else boto3.client("secretsmanager")
        return secrets_client

    def handler(event: Any, context: Any) -> dict[str, Any]:
        def log_failure(stage: str, outcome: str, exc: Exception | None = None) -> None:
            record = {"event": "data_source_failure", "source": source_id, "stage": stage,
                      "outcome": outcome, "request_id": getattr(context, "aws_request_id", "unknown")}
            if exc is not None:
                record["error_type"] = type(exc).__name__
            logger.warning(json.dumps(record))

        tool_name = tool_name_from_context(context)
        if tool_name is not None and not tool_name.endswith(TOOL_SUFFIX):
            log_failure("validate", "unknown_tool")
            return _error_result(tool, "unknown_tool", f"This target only serves a '{TOOL_SUFFIX[3:]}' tool.")
        if not isinstance(event, dict):
            log_failure("validate", "invalid_input")
            return _error_result(tool, "invalid_input", "The tool arguments must be a JSON object.")
        try:
            # Extra keys (for example an `api_key` or `user_id` supplied by the agent) are dropped here.
            args = tool.InputSchema.model_validate(event).model_dump()
        except ValidationError as exc:
            log_failure("validate", "invalid_input")
            fields = sorted({".".join(str(p) for p in err["loc"]) for err in exc.errors()})
            return _error_result(tool, "invalid_input", "The tool arguments are not valid.", fields=fields)

        used_user_key = False
        if tool.supports_api_key:
            user_id = user_id_resolver(event, context)
            if user_id:
                prefix = os.environ.get(SECRET_PREFIX_ENV, DEFAULT_SECRET_PREFIX)
                api_key = lookup_user_api_key(_secrets(), user_id, key_source_id, prefix)
                if api_key:
                    args["api_key"] = api_key
                    used_user_key = True

        try:
            result = tool.fetch(**args)
        except Exception as exc:
            log_failure("fetch", "exception", exc)
            raise
        payload = result.model_dump(mode="json")
        payload["query_metadata"]["user_api_key"] = used_user_key
        if "error" in result.query_metadata:
            log_failure("fetch", "upstream_error")
        logger.info(
            json.dumps(
                {
                    "event": "data_source_call",
                    "source": source_id,
                    "results": len(result.results),
                    "error": "error" in result.query_metadata,
                    "user_api_key": used_user_key,
                }
            )
        )
        return payload

    return handler
