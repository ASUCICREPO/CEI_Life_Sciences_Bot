"""Short-term memory for model calls: the earlier turns of this chat, bounded.

Each agent's state["messages"] already holds the chat (a warm in-process checkpoint, a restored
checkpoint snapshot, or ChatHistoryStore rehydration in `begin_turn`). Nodes that call a model with
only `last_human_message(state)` lose that context, so a follow-up question has no memory of past
work. `user_turn_with_history` puts the earlier turns in front of this turn's user text.

B51/B51a: earlier user text is user-typed, so it stays in guard_content blocks, the same as the
current query. Earlier assistant text is agent-generated, so it is plain text.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from shared.response_text import content_text
from shared.runtime.guarded_input import Guarded, Segment, content_blocks

DEFAULT_MAX_TURNS = 10
DEFAULT_MAX_CHARS = 12_000
DEFAULT_MAX_MESSAGE_CHARS = 2_000

HISTORY_HEADER = "Earlier conversation in this chat (oldest first; use it to resolve follow-up references):\n"


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _prior_messages(messages: Sequence[BaseMessage]) -> list[BaseMessage]:
    """Every user/assistant message before the newest HumanMessage (this turn's prompt)."""
    last_human = next((i for i in range(len(messages) - 1, -1, -1) if isinstance(messages[i], HumanMessage)), None)
    if last_human is None:
        return []
    return [m for m in messages[:last_human] if isinstance(m, (HumanMessage, AIMessage))]


def prior_conversation_segments(
    state: Mapping[str, Any],
    *,
    max_turns: int = DEFAULT_MAX_TURNS,
    max_chars: int = DEFAULT_MAX_CHARS,
    max_message_chars: int = DEFAULT_MAX_MESSAGE_CHARS,
) -> list[Segment]:
    """The newest earlier turns as content segments; [] when this is the first turn.

    Keeps at most `max_turns` user messages (with their replies) and at most `max_chars` of text,
    dropping the oldest first. Each message is clipped to `max_message_chars`.
    """
    prior = _prior_messages(state.get("messages") or [])
    entries: list[tuple[bool, str]] = []
    humans = 0
    used = 0
    for message in reversed(prior):
        is_user = isinstance(message, HumanMessage)
        text = _clip(content_text(message.content), max_message_chars)
        if not text:
            continue
        if is_user:
            if humans >= max_turns:
                break
            humans += 1
        if used + len(text) > max_chars:
            break
        used += len(text)
        entries.append((is_user, text))
    if not entries:
        return []
    segments: list[Segment] = [HISTORY_HEADER]
    for is_user, text in reversed(entries):
        if is_user:
            segments += ["User: ", Guarded(text), "\n"]
        else:
            segments.append(f"Assistant: {text}\n")
    segments.append("\n")
    return segments


def user_turn_with_history(
    state: Mapping[str, Any], user_text: str, *, before: str = "", after: str = "", **limits: int,
) -> list[dict[str, str]]:
    """Like `user_turn(user_text, before=..., after=...)`, with the earlier turns first."""
    return content_blocks(*prior_conversation_segments(state, **limits), before, Guarded(user_text), after)
