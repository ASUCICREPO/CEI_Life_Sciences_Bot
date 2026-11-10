"""Validate an authenticated request for an export of a completed chat turn.

This module prepares trusted input for an export worker. The caller must first
authenticate the request and verify session ownership. It does not queue work or
expose an endpoint: the worker and its deployment still need to be wired.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from http_errors import invalid_request, not_found
from shared.persistence.chat_history import ChatHistoryRepository, MessageRole
from shared.persistence.ownership import is_session_uuid

_TURN_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_FORMATS = frozenset({"pdf", "docx", "md", "csv", "latex", "txt"})


@dataclass(frozen=True)
class ReviewedExportRequest:
    user_id: str
    session_id: str
    turn_id: str
    format: str
    text: str


def prepare_reviewed_export(
    *, user_id: str, session_id: str, turn_id: str, body: dict[str, Any], history: ChatHistoryRepository,
    turn_status: str | None = None,
) -> ReviewedExportRequest:
    """Require an explicit acknowledgment and load only the caller's completed AI text.

    `user_id` must come from Cognito claims; the caller must separately verify both
    session ownership and metadata existence before calling. Text is loaded from
    persisted history, never from the request body, model, or tool arguments.
    `turn_status` must come from a durable server-side completion record. Current
    history records do not have such a field, so callers must fail closed until
    that record is available.
    """
    if not user_id or not is_session_uuid(session_id) or not isinstance(turn_id, str) or not _TURN_ID.fullmatch(turn_id):
        raise invalid_request("The export request is not valid.")
    if not isinstance(body, dict) or set(body) != {"format", "reviewed"} or body.get("reviewed") is not True:
        raise invalid_request("Review acknowledgment is required for export.")
    format = body.get("format")
    if not isinstance(format, str) or format not in _FORMATS:
        raise invalid_request("The export format is not supported.")
    if turn_status != "completed":
        raise not_found("The completed turn was not found.")

    cursor = None
    while True:
        page = history.load_page(user_id, session_id, limit=100, start_key=cursor)
        for turn in page.items:
            if turn.message_id != turn_id:
                continue
            if turn.role != MessageRole.AI or turn.user_id not in (None, user_id):
                raise not_found("The completed turn was not found.")
            text = "\n\n".join(block.body for block in turn.content_blocks
                               if block.block_type == "text" and isinstance(block.body, str)).strip()
            if not text:
                raise not_found("The completed turn was not found.")
            return ReviewedExportRequest(user_id=user_id, session_id=session_id, turn_id=turn_id, format=format, text=text)
        if not page.next_key:
            raise not_found("The completed turn was not found.")
        cursor = page.next_key
