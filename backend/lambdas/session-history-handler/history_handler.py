"""Session History Handler (api.md, F3/F6).

  GET /sessions/{sessionId}/history                     turns only
  GET /sessions/{sessionId}/turns/{messageId}/trace     one AI turn's redacted reasoning/tool trace (B59, lazy)
"""

from __future__ import annotations

import re
from collections.abc import Callable
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
from shared.persistence.chat_metadata import ChatMetadataRecord, ChatMetadataRepository, SessionNotFoundError
from shared.persistence.ownership import OwnershipCheckError, is_session_uuid, verify_session_owner
from shared.persistence.traces import TraceRepository

logger = get_logger(__name__)

HISTORY_ROUTE = ("GET", "/sessions/{sessionId}/history")
TRACE_ROUTE = ("GET", "/sessions/{sessionId}/turns/{messageId}/trace")
TRACE_CACHE_CONTROL = "private, max-age=300"
_MESSAGE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

_metadata: ChatMetadataRepository | None = None
_history: ChatHistoryRepository | None = None
_traces: TraceRepository | None = None


def _repositories() -> tuple[ChatMetadataRepository, ChatHistoryRepository]:
    global _metadata, _history
    if _metadata is None:
        _metadata = ChatMetadataRepository()
    if _history is None:
        _history = ChatHistoryRepository()
    return _metadata, _history


def _trace_repository() -> TraceRepository:
    global _traces
    if _traces is None:
        _traces = TraceRepository()
    return _traces


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_turn_sequence(value: Any) -> bool:
    # turns are >= 0; negative sort keys are reserved for checkpoint/trace items and are never valid page cursors
    return _is_int(value) and value >= 0


def _authorized_session(event: dict[str, Any], context: Any, user_id: str) -> tuple[str, ChatMetadataRecord]:
    """Validate sessionId, then prove ownership before anything else reads the session (fail closed)."""
    session_id = (event.get("pathParameters") or {}).get("sessionId")
    if not is_session_uuid(session_id):
        raise invalid_request("The sessionId path parameter is not valid.")

    metadata, history = _repositories()
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
    return session_id, session


def _get_history(event: dict[str, Any], context: Any, user_id: str) -> dict[str, Any]:
    session_id, session = _authorized_session(event, context, user_id)
    _, history = _repositories()

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


def _get_trace(event: dict[str, Any], context: Any, user_id: str) -> dict[str, Any]:
    message_id = (event.get("pathParameters") or {}).get("messageId")
    if not isinstance(message_id, str) or not _MESSAGE_ID.match(message_id):
        raise invalid_request("The messageId path parameter is not valid.")
    session_id, _ = _authorized_session(event, context, user_id)

    events = _trace_repository().load(user_id, session_id, message_id)
    if events is None:
        raise not_found("The trace was not found.")
    response = json_response(200, {"message_id": message_id, "events": events}, event)
    response["headers"]["Cache-Control"] = TRACE_CACHE_CONTROL
    return response


# Strategy registry: (method, resource) -> route function
ROUTES: dict[tuple[str, str], Callable[[dict[str, Any], Any, str], dict[str, Any]]] = {
    HISTORY_ROUTE: _get_history,
    TRACE_ROUTE: _get_trace,
}


@api_handler(logger)
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    user_id = user_id_from_claims(event)
    route = ROUTES.get((event.get("httpMethod"), event.get("resource")))
    if route is None:
        raise ApiError(404, "not_found", "The route was not found.")
    return route(event, context, user_id)
