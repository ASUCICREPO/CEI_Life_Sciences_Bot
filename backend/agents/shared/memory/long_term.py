"""AgentCore long-term memory primitives; callers must supply verified identities.

Research reads through `turns.py` behind an off-by-default flag; no agent writes
events yet. See LONG_TERM.md for the deletion-fencing and live-proof gates.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")
MAX_RECORDS = 10
MAX_RECORD_CHARS = 2000


def _identifier(value: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError("invalid memory identity")
    return value


def user_namespace(user_id: str) -> str:
    """Exact B34 per-user namespace, with no caller-supplied path segments."""
    return f"/users/{_identifier(user_id)}/"


def fetch_long_term_context(*, client: Any, memory_id: str | None, user_id: str,
                            query: str, limit: int = MAX_RECORDS) -> list[str]:
    """Retrieve bounded, relevant memory records for exactly one verified user.

    Memory is untrusted conversation data; agents must not treat its contents as
    system instructions. The caller is responsible for authenticating `user_id`.
    """
    namespace = user_namespace(user_id)
    if not memory_id:
        return []
    if not isinstance(query, str) or not query.strip() or len(query) > 16000:
        raise ValueError("invalid memory query")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_RECORDS:
        raise ValueError("invalid memory limit")
    response = client.retrieve_memory_records(memoryId=memory_id, namespace=namespace,
        searchCriteria={"searchQuery": query, "topK": limit}, maxResults=limit)
    records = response.get("memoryRecordSummaries", [])
    if not isinstance(records, list):
        raise ValueError("invalid memory response")
    result = []
    for record in records[:limit]:
        if not isinstance(record, dict):
            raise ValueError("invalid memory record")
        namespaces = record.get("namespaces")
        if namespaces is not None and (not isinstance(namespaces, list) or namespace not in namespaces):
            raise ValueError("unexpected memory namespace")
        content = record.get("content", {})
        text = content.get("text") if isinstance(content, dict) else None
        if isinstance(text, str) and text.strip():
            result.append(text[:MAX_RECORD_CHARS])
    return result


def record_conversation(*, client: Any, memory_id: str | None, user_id: str, session_id: str,
                        user_message: str, assistant_message: str, event_time: datetime,
                        idempotency_token: str) -> None:
    """Store conversational events so configured built-in strategies can extract memories.

    Use a stable unique turn-derived token on retries. Only completed, approved
    conversation text belongs here; never pass tools, secrets, or hidden traces.
    """
    _identifier(user_id)
    _identifier(session_id)
    if not memory_id:
        return
    if (not isinstance(user_message, str) or not user_message.strip() or len(user_message) > 16000
            or not isinstance(assistant_message, str) or not assistant_message.strip()
            or len(assistant_message) > 100000 or not isinstance(event_time, datetime)
            or event_time.tzinfo is None or not isinstance(idempotency_token, str)
            or not 1 <= len(idempotency_token) <= 256):
        raise ValueError("invalid conversation event")
    client.create_event(memoryId=memory_id, actorId=user_id, sessionId=session_id,
        eventTimestamp=event_time, clientToken=idempotency_token,
        payload=[{"conversational": {"role": "USER", "content": {"text": user_message}}},
                 {"conversational": {"role": "ASSISTANT", "content": {"text": assistant_message}}}])
