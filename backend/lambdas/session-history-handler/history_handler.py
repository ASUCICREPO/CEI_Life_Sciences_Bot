"""Session History Handler: GET /sessions/{sessionId}/history (turns only; api.md, F3)."""

from __future__ import annotations

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
    parse_limit,
    request_id,
    user_id_from_claims,
)
from shared.persistence.chat_history import ChatHistoryRepository
from shared.persistence.chat_metadata import ChatMetadataRepository, SessionNotFoundError
from shared.persistence.ownership import OwnershipCheckError, is_session_uuid, verify_session_owner

logger = get_logger(__name__)

_metadata: ChatMetadataRepository | None = None
_history: ChatHistoryRepository | None = None


def _repositories() -> tuple[ChatMetadataRepository, ChatHistoryRepository]:
    global _metadata, _history
    if _metadata is None:
        _metadata = ChatMetadataRepository()
    if _history is None:
        _history = ChatHistoryRepository()
    return _metadata, _history


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_turn_sequence(value: Any) -> bool:
    # turns are >= 0; negative sort keys are reserved for checkpoint items and are never valid page cursors
    return _is_int(value) and value >= 0


@api_handler(logger)
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    user_id = user_id_from_claims(event)
    if (event.get("httpMethod"), event.get("resource")) != ("GET", "/sessions/{sessionId}/history"):
        raise ApiError(404, "not_found", "The route was not found.")

    session_id = (event.get("pathParameters") or {}).get("sessionId")
    if not is_session_uuid(session_id):
        raise invalid_request("The sessionId path parameter is not valid.")

    metadata, history = _repositories()
    # Ownership first, before anything else reads the session; fail closed if the owner cannot be read.
    # B12: the history table is keyed by session_id only, so the stamped owner is checked, then the metadata row
    # (keyed by the caller) must exist too.
    try:
        verify_session_owner(history, user_id, session_id)
    except SessionNotFoundError:
        raise not_found("The session was not found.") from None
    except OwnershipCheckError:
        logger.exception(
            "session ownership check failed; request refused",
            extra={"request_id": request_id(event, context), "route": event.get("resource")},
        )
        raise ApiError(500, "internal_error", GENERIC_INTERNAL_ERROR) from None
    session = metadata.get(user_id, session_id)
    if session is None:
        raise not_found("The session was not found.")

    params = event.get("queryStringParameters") or {}
    limit = parse_limit(params)
    start_key = decode_cursor(
        params.get("cursor"),
        {"session_id": lambda v: v == session_id, "sequence_number": _is_turn_sequence},
    )

    # Owner-scoped read: items stamped with another user_id are dropped as well (defence in depth)
    page = history.load_page(user_id, session_id, limit=limit, start_key=start_key)
    items = [
        record.model_dump(
            mode="json", exclude={"session_id", "user_id", "tool_name", "tool_input", "tool_output"}
        )
        for record in page.items
    ]
    envelope = page_envelope(items, page.next_key)
    # lets the UI reopen the chat in the right agent / mode / model
    envelope["session"] = session.model_dump(
        mode="json",
        include={
            "session_id",
            "title",
            "title_source",
            "agent_type",
            "mode",
            "model_id",
            "document_type",
            "analysis_type",
        },
    )
    return json_response(200, envelope, event)
