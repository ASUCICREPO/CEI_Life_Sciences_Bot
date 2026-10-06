"""Session Metadata Handler: POST/GET /sessions, GET/PATCH /sessions/{sessionId} (api.md, F3)."""

from __future__ import annotations

import re
import secrets
import time
from datetime import UTC, datetime
from typing import Any

from http_errors import (
    ApiError,
    api_handler,
    decode_cursor,
    get_logger,
    invalid_request,
    json_response,
    not_found,
    page_envelope,
    parse_json_body,
    parse_limit,
    user_id_from_claims,
)
from shared.persistence.chat_metadata import (
    ChatMetadataRecord,
    ChatMetadataRepository,
    ChatMode,
    SessionNotFoundError,
)

logger = get_logger(__name__)

TITLE_MAX = 200
MODEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

_repo: ChatMetadataRepository | None = None


def _repository() -> ChatMetadataRepository:
    global _repo
    if _repo is None:
        _repo = ChatMetadataRepository()
    return _repo


def _new_session_id() -> str:
    # ADR: time-ordered id (ms timestamp + random) | Rationale: the sort key then lists sessions newest-first without a GSI
    return f"{int(time.time() * 1000):012x}-{secrets.token_hex(8)}"


def _validate_title(value: Any) -> str:
    if not isinstance(value, str):
        raise invalid_request("The title field must be a string.")
    title = value.strip()
    if not 1 <= len(title) <= TITLE_MAX or _CONTROL_CHARS.search(title):
        raise invalid_request(f"The title field must be 1 to {TITLE_MAX} characters.")
    return title


def _validate_mode(value: Any) -> ChatMode:
    if value not in {m.value for m in ChatMode}:
        raise invalid_request("The mode field must be 'science' or 'business'.")
    return ChatMode(value)


def _validate_model_id(value: Any) -> str:
    if not isinstance(value, str) or not MODEL_ID_PATTERN.match(value):
        raise invalid_request("The model_id field is not a valid model identifier.")
    return value


def _reject_unknown(body: dict[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise invalid_request(f"Unknown field: {unknown[0]}.")


def _session_id_param(event: dict[str, Any]) -> str:
    session_id = (event.get("pathParameters") or {}).get("sessionId")
    if not isinstance(session_id, str) or not SESSION_ID_PATTERN.match(session_id):
        raise invalid_request("The sessionId path parameter is not valid.")
    return session_id


def _public(record: ChatMetadataRecord) -> dict[str, Any]:
    body = record.model_dump(mode="json", exclude={"user_id"})
    return body


def _create(event: dict[str, Any], user_id: str) -> dict[str, Any]:
    body = parse_json_body(event)
    _reject_unknown(body, {"title", "mode", "model_id"})
    if "title" not in body or "mode" not in body:
        raise invalid_request("The title and mode fields are required.")
    now = datetime.now(UTC)
    record = ChatMetadataRecord(
        session_id=_new_session_id(),
        user_id=user_id,
        title=_validate_title(body["title"]),
        mode=_validate_mode(body["mode"]),
        model_id=_validate_model_id(body["model_id"]) if body.get("model_id") is not None else None,
        created_at=now,
        updated_at=now,
    )
    _repository().create(record)
    created = record.model_dump(mode="json")
    response = {k: created[k] for k in ("session_id", "title", "mode", "status", "created_at")}
    return json_response(201, response, event)


def _list(event: dict[str, Any], user_id: str) -> dict[str, Any]:
    params = event.get("queryStringParameters") or {}
    limit = parse_limit(params)
    start_key = decode_cursor(
        params.get("cursor"),
        {"user_id": lambda v: v == user_id, "session_id": lambda v: isinstance(v, str)},
    )
    page = _repository().list_by_user(user_id, limit=limit, start_key=start_key)
    return json_response(200, page_envelope([_public(r) for r in page.items], page.next_key), event)


def _get(event: dict[str, Any], user_id: str) -> dict[str, Any]:
    record = _repository().get(user_id, _session_id_param(event))
    if record is None:
        raise not_found("The session was not found.")
    return json_response(200, _public(record), event)


def _patch(event: dict[str, Any], user_id: str) -> dict[str, Any]:
    session_id = _session_id_param(event)
    body = parse_json_body(event)
    _reject_unknown(body, {"title", "mode"})
    if not body:
        raise invalid_request("Provide at least one of title or mode.")
    title = _validate_title(body["title"]) if "title" in body else None
    mode = _validate_mode(body["mode"]) if "mode" in body else None
    try:
        record = _repository().update(user_id, session_id, title=title, mode=mode)
    except SessionNotFoundError:
        raise not_found("The session was not found.") from None
    return json_response(200, _public(record), event)


ROUTES = {
    ("POST", "/sessions"): _create,
    ("GET", "/sessions"): _list,
    ("GET", "/sessions/{sessionId}"): _get,
    ("PATCH", "/sessions/{sessionId}"): _patch,
}


@api_handler(logger)
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    user_id = user_id_from_claims(event)
    route = ROUTES.get((event.get("httpMethod"), event.get("resource")))
    if route is None:
        raise ApiError(404, "not_found", "The route was not found.")
    return route(event, user_id)
