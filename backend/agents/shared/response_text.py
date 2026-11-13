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


def content_text(content: Any) -> str:
    """Plain text of a message `content`: a string, or the text of Bedrock/Anthropic content blocks.

    Streamed Converse chunks carry `[{"type": "text", "text": ..., "index": 0}]`; `str()` of that
    list leaks the Python repr into the chat, so only text blocks are joined here.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    return str(content)


def final_ai_text(messages: Sequence[BaseMessage]) -> str | None:
    """Return the final non-empty assistant message, ignoring trailing tool messages."""
    for message in reversed(messages):
        if not isinstance(message, AIMessage):
            continue
        content = message.content
        text = content_text(content)
        if text.strip() and not _is_non_response_payload(content, text):
            return text
    return None