"""Helpers for selecting user-facing output from completed agent states."""
from __future__ import annotations

from collections.abc import Sequence
import json
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage


def _is_non_response_payload(content: Any, text: str) -> bool:
    if content == []:
        return True

    try:
        payload = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return False

    if payload == []:
        return True
    return isinstance(payload, dict) and (
        "tool_calls" in payload
        or ("name" in payload and ("args" in payload or "arguments" in payload))
    )


def final_ai_text(messages: Sequence[BaseMessage]) -> str | None:
    """Return the final non-empty assistant message, ignoring trailing tool messages."""
    for message in reversed(messages):
        if not isinstance(message, AIMessage):
            continue
        content = message.content
        text = content if isinstance(content, str) else str(content)
        if text.strip() and not _is_non_response_payload(content, text):
            return text
    return None