"""Session History Handler: GET /sessions/{sessionId}/history (turns only; api.md, F3)."""

from __future__ import annotations

import re
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
    parse_limit,
    user_id_from_claims,
)
from shared.persistence.chat_history import ChatHistoryRepository
from shared.persistence.chat_metadata import ChatMetadataRepository

logger = get_logger(__name__)

SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

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


@api_handler(logger)
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    user_id = user_id_from_claims(event)
    if (event.get("httpMethod"), event.get("resource")) != ("GET", "/sessions/{sessionId}/history"):
        raise ApiError(404, "not_found", "The route was not found.")

    session_id = (event.get("pathParameters") or {}).get("sessionId")
    if not isinstance(session_id, str) or not SESSION_ID_PATTERN.match(session_id):
        raise invalid_request("The sessionId path parameter is not valid.")
    params = event.get("queryStringParameters") or {}
    limit = parse_limit(params)
    start_key = decode_cursor(
        params.get("cursor"),
        {"session_id": lambda v: v == session_id, "sequence_number": _is_int},
    )

    metadata, history = _repositories()
    # B12: the history table is keyed by session_id only, so ownership is proven through the metadata table first
    if metadata.get(user_id, session_id) is None:
        raise not_found("The session was not found.")

    page = history.load_page(session_id, limit=limit, start_key=start_key)
    items = [
        record.model_dump(mode="json", exclude={"session_id", "tool_name", "tool_input", "tool_output"})
        for record in page.items
    ]
    return json_response(200, page_envelope(items, page.next_key), event)
