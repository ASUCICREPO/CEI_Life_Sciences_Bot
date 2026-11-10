"""Session Metadata Handler: POST/GET /sessions, GET/PATCH /sessions/{sessionId} (api.md, F3)."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

from http_errors import (
    GENERIC_INTERNAL_ERROR,
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
    request_id,
    user_id_from_claims,
)
from shared.config import config
from shared.persistence.chat_history import ChatHistoryRepository
from shared.persistence.chat_metadata import (
    SEARCH_QUERY_MAX,
    SORT_ATTRIBUTE,
    TITLE_SEARCH_ATTRIBUTE,
    ChatMetadataRecord,
    ChatMetadataRepository,
    ChatMode,
    SessionNotFoundError,
    SortKey,
    normalize_title,
)
from shared.persistence.ownership import OwnershipCheckError, is_session_uuid, verify_session_owner
from shared.runtime import model_discovery

logger = get_logger(__name__)

_bedrock_client: Any | None = None


def _bedrock() -> Any:
    global _bedrock_client
    if _bedrock_client is None:
        import boto3

        _bedrock_client = boto3.client("bedrock", region_name=config.aws_region)
    return _bedrock_client


TITLE_MAX = 200
PAGE_SIZE = 25  # chat history page size (GET /sessions default)
MODEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

_repo: ChatMetadataRepository | None = None
_history: ChatHistoryRepository | None = None


def _repository() -> ChatMetadataRepository:
    global _repo
    if _repo is None:
        _repo = ChatMetadataRepository()
    return _repo


def _history_repository() -> ChatHistoryRepository:
    global _history
    if _history is None:
        _history = ChatHistoryRepository()
    return _history


def _new_session_id() -> str:
    # ADR: a dedicated random UUID per session, never a default | Rationale: the same id is the AgentCore
    # runtimeSessionId (>= 33 chars; a UUID is 36) and every entry point accepts UUIDs only. List ordering comes
    # from the GSIs, so the id no longer needs a time prefix.
    return str(uuid.uuid4())


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
    # B18/MODEL_POLICY.md step 2: fail closed on both the blocklist and server-side discovery; an
    # explicitly rejected selection must not silently fall back to the default (shared/runtime/turn_config.py).
    if not model_discovery.is_model_available(value, _bedrock(), config.model_blocklist):
        raise invalid_request("The model_id field is not an available model.")
    return value


def _reject_unknown(body: dict[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise invalid_request(f"Unknown field: {unknown[0]}.")


def _session_id_param(event: dict[str, Any]) -> str:
    session_id = (event.get("pathParameters") or {}).get("sessionId")
    if not is_session_uuid(session_id):
        raise invalid_request("The sessionId path parameter is not valid.")
    return session_id


def _owned_session_id(event: dict[str, Any], user_id: str) -> str:
    """Validate the path id, then prove ownership before any other read or write of the session (fail closed)."""
    session_id = _session_id_param(event)
    try:
        verify_session_owner(_history_repository(), user_id, session_id)
    except SessionNotFoundError:
        raise not_found("The session was not found.") from None
    except OwnershipCheckError:
        logger.exception(
            "session ownership check failed; request refused",
            extra={"request_id": request_id(event), "route": event.get("resource")},
        )
        raise ApiError(500, "internal_error", GENERIC_INTERNAL_ERROR) from None
    return session_id


def _public(record: ChatMetadataRecord) -> dict[str, Any]:
    body = record.model_dump(mode="json", exclude={"user_id"})
    return body


def _create(event: dict[str, Any], user_id: str) -> dict[str, Any]:
    body = parse_json_body(event)
    _reject_unknown(body, {"title", "mode", "model_id", "agent_type"})
    if "title" not in body or "mode" not in body:
        raise invalid_request("The title and mode fields are required.")
    now = datetime.now(UTC)
    # Validate agent_type when provided — must be a known value so History can resume the session
    _KNOWN_AGENT_TYPES = frozenset({"research", "drafting", "analysis", "pipeline_manager"})
    agent_type_raw = body.get("agent_type")
    agent_type: str | None = None
    if agent_type_raw is not None:
        if not isinstance(agent_type_raw, str) or agent_type_raw not in _KNOWN_AGENT_TYPES:
            raise invalid_request(
                "The agent_type field must be one of: research, drafting, analysis, pipeline_manager."
            )
        agent_type = agent_type_raw
    # A brand-new UUID has no history, so the owner is the caller by construction (stamped on the first turn)
    record = ChatMetadataRecord(
        session_id=_new_session_id(),
        user_id=user_id,
        title=_validate_title(body["title"]),
        mode=_validate_mode(body["mode"]),
        model_id=_validate_model_id(body["model_id"]) if body.get("model_id") is not None else None,
        agent_type=agent_type,
        created_at=now,
        updated_at=now,
        last_activity_at=now,
    )
    _repository().create(record)
    created = record.model_dump(mode="json")
    response = {
        k: created[k]
        for k in ("session_id", "title", "title_source", "mode", "status", "created_at")
    }
    return json_response(201, response, event)


def _sort_param(params: dict[str, Any]) -> SortKey:
    raw = params.get("sort") or SortKey.ACTIVITY.value
    if raw not in {s.value for s in SortKey}:
        raise invalid_request("The sort parameter must be 'activity', 'updated' or 'created'.")
    return SortKey(raw)


def _search_param(params: dict[str, Any]) -> str | None:
    raw = params.get("q")
    if raw is None:
        return None
    if not isinstance(raw, str) or _CONTROL_CHARS.search(raw) or len(raw) > SEARCH_QUERY_MAX:
        raise invalid_request(f"The q parameter must be at most {SEARCH_QUERY_MAX} characters.")
    return normalize_title(raw) or None


def _list(event: dict[str, Any], user_id: str) -> dict[str, Any]:
    params = event.get("queryStringParameters") or {}
    limit = parse_limit(params, default=PAGE_SIZE)
    sort = _sort_param(params)
    query = _search_param(params)
    # A GSI cursor is {user_id, session_id, <index sort attribute>}; the partition key is pinned to the caller,
    # and a cursor minted under another order (or for a search vs. the plain list) has the wrong shape
    sort_attribute = TITLE_SEARCH_ATTRIBUTE if query else SORT_ATTRIBUTE[sort]
    start_key = decode_cursor(
        params.get("cursor"),
        {
            "user_id": lambda v: v == user_id,
            "session_id": lambda v: isinstance(v, str),
            sort_attribute: lambda v: isinstance(v, str),
        },
    )
    page = _repository().list_by_user(
        user_id, sort=sort, limit=limit, start_key=start_key, query=query
    )
    return json_response(200, page_envelope([_public(r) for r in page.items], page.next_key), event)


def _get(event: dict[str, Any], user_id: str) -> dict[str, Any]:
    session_id = _owned_session_id(event, user_id)
    record = _repository().get(user_id, session_id)
    if record is None:
        raise not_found("The session was not found.")
    return json_response(200, _public(record), event)


def _patch(event: dict[str, Any], user_id: str) -> dict[str, Any]:
    session_id = _owned_session_id(event, user_id)
    body = parse_json_body(event)
    _reject_unknown(body, {"title", "mode"})
    if not body:
        raise invalid_request("Provide at least one of title or mode.")
    title = (
        _validate_title(body["title"]) if "title" in body else None
    )  # repo marks title_source="user"
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
