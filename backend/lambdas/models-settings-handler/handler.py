"""Models + Settings Handler: GET /models, GET/PUT /settings (api.md, F8).

GET /models lists the account's enabled, non-blocklisted Bedrock models (B18). GET/PUT /settings
reads/writes `default_model_id`/`default_mode` on the UserMetadataTable (data-model.md Section 4,
first real use of that table). A PUT model_id must be both un-blocklisted and server-discovered
(fail closed, MODEL_POLICY.md step 2) — an explicitly rejected selection never silently falls back.
"""
from __future__ import annotations

import re
from typing import Any

from http_errors import (
    ApiError,
    api_handler,
    get_logger,
    invalid_request,
    json_response,
    parse_json_body,
    user_id_from_claims,
)
from shared.config import config
from shared.persistence.chat_metadata import ChatMode
from shared.persistence.user_metadata import UserMetadataRecord, UserMetadataRepository
from shared.runtime import model_discovery

logger = get_logger(__name__)

# Same shape as session-metadata-handler's MODEL_ID_PATTERN; kept local so this Lambda has no
# import-time dependency on that handler's module.
MODEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")

_repo: UserMetadataRepository | None = None
_bedrock_client: Any | None = None


def _repository() -> UserMetadataRepository:
    global _repo
    if _repo is None:
        _repo = UserMetadataRepository()
    return _repo


def _bedrock() -> Any:
    global _bedrock_client
    if _bedrock_client is None:
        import boto3

        _bedrock_client = boto3.client("bedrock", region_name=config.aws_region)
    return _bedrock_client


def _permitted_models() -> list[model_discovery.DiscoveredModel]:
    return model_discovery.permitted_models(_bedrock(), config.model_blocklist)


def _validate_model_id(value: Any) -> str:
    if not isinstance(value, str) or not MODEL_ID_PATTERN.match(value):
        raise invalid_request("The default_model_id field is not a valid model identifier.")
    if not any(model.model_id == value for model in _permitted_models()):
        raise invalid_request("The default_model_id field is not an available model.")
    return value


def _validate_mode(value: Any) -> ChatMode:
    if value not in {m.value for m in ChatMode}:
        raise invalid_request("The default_mode field must be 'science' or 'business'.")
    return ChatMode(value)


def _reject_unknown(body: dict[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise invalid_request(f"Unknown field: {unknown[0]}.")


def _public_settings(record: UserMetadataRecord | None) -> dict[str, Any]:
    return {
        "default_model_id": record.default_model_id if record is not None else None,
        "default_mode": record.default_mode.value if record is not None and record.default_mode is not None else None,
        # F18 (user API keys) populates this from the per-user Secrets Manager key references; empty until then.
        "configured_sources": [],
    }


def _get_models(event: dict[str, Any], _user_id: str) -> dict[str, Any]:
    models = [{"model_id": model.model_id, "label": model.label} for model in _permitted_models()]
    return json_response(200, {"models": models}, event)


def _get_settings(event: dict[str, Any], user_id: str) -> dict[str, Any]:
    return json_response(200, _public_settings(_repository().get(user_id)), event)


def _put_settings(event: dict[str, Any], user_id: str) -> dict[str, Any]:
    body = parse_json_body(event)
    _reject_unknown(body, {"default_model_id", "default_mode"})
    model_id = _validate_model_id(body["default_model_id"]) if body.get("default_model_id") is not None else None
    mode = _validate_mode(body["default_mode"]) if body.get("default_mode") is not None else None
    record = _repository().put(user_id, default_model_id=model_id, default_mode=mode)
    return json_response(200, _public_settings(record), event)


ROUTES = {
    ("GET", "/models"): _get_models,
    ("GET", "/settings"): _get_settings,
    ("PUT", "/settings"): _put_settings,
}


@api_handler(logger)
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    user_id = user_id_from_claims(event)
    route = ROUTES.get((event.get("httpMethod"), event.get("resource")))
    if route is None:
        raise ApiError(404, "not_found", "The route was not found.")
    return route(event, user_id)
