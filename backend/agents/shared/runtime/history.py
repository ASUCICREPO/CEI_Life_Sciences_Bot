"""Conversation persistence hooks shared by the four AgentCore entrypoints.

Each entrypoint calls, in order:
  history = await begin_turn(...)   # ownership check + record prompt (+ restore a cold thread)
  ...graph.ainvoke(initial_state with history + [HumanMessage(prompt)])...
  await end_turn(...)               # record the answer + schedule the checkpoint snapshot (never raises)
HITL resumes call `record_resume` before resuming the graph, then `end_turn` with the resumed envelope.

Cold-thread restore order (plan 3b4d4f64 decision 10): the full LangGraph checkpoint from ChatHistoryStore first
(shared/runtime/checkpointing.py); only if none is usable, the last `history_rehydrate_turns` text turns.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Mapping, Sequence
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from shared.config import config
from shared.persistence.chat_metadata import SessionNotFoundError
from shared.persistence.conversation import ConversationRecorder
from shared.persistence.ownership import OwnershipCheckError
from shared.runtime import checkpointing

if TYPE_CHECKING:
    from langchain_core.messages import BaseMessage
    from langgraph.checkpoint.base import BaseCheckpointSaver

    from shared.persistence.traces import TraceRepository
    from shared.schemas.cei_ui import ActionRequest

log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_recorder() -> ConversationRecorder:
    """Process-wide recorder (module-level instance, built on first use so imports stay side-effect free)."""
    from shared.persistence.chat_history import ChatHistoryRepository
    from shared.persistence.chat_metadata import ChatMetadataRepository
    from shared.runtime.titles import bedrock_title_generator

    return ConversationRecorder(ChatMetadataRepository(), ChatHistoryRepository(), bedrock_title_generator())


def citations_from(retrieved_results: Sequence[object] | None) -> list[dict[str, Any]]:
    """Deduplicated https citations from the retrieval results (same rules as the CEI evidence panel)."""
    if not retrieved_results:
        return []
    from shared.presenters.cei_ui import _collect_citations  # single source of the citation rules

    citations, _ = _collect_citations(retrieved_results)
    return [c.model_dump(mode="json") for c in citations]


async def begin_turn(
    *,
    user_id: str,
    session_id: str,
    prompt: str,
    agent_type: str,
    mode: str | None,
    warm: bool,
    model_id: str | None = None,
    document_type: str | None = None,
    analysis_type: str | None = None,
    recorder: ConversationRecorder | None = None,
    saver: BaseCheckpointSaver | None = None,
) -> list[BaseMessage]:
    """Record the prompt; return prior messages to prepend when the in-memory thread is cold (else []).

    On a cold thread with a `saver`, the stored checkpoint is restored into it first; then no text history is
    returned because the restored state already holds the conversation.

    Raises SessionNotFoundError when the session belongs to someone else, and OwnershipCheckError when the owner
    cannot be read: the ownership check never fails open. Any other persistence failure (recording the prompt) is
    logged and the turn proceeds without history — ownership was already proven by the session guard.
    """
    rec = recorder or get_recorder()
    try:
        await rec.start_turn(
            user_id=user_id,
            session_id=session_id,
            prompt=prompt,
            agent_type=agent_type,
            mode=mode,
            model_id=model_id,
            document_type=document_type,
            analysis_type=analysis_type,
        )
    except (SessionNotFoundError, OwnershipCheckError):
        raise
    except Exception as exc:  # noqa: BLE001
        log.error("chat history: could not record prompt session=%s error_type=%s", session_id, type(exc).__name__)
        return []
    if warm:
        return []
    if saver is not None and await checkpointing.restore_checkpoint(
        user_id=user_id, session_id=session_id, saver=saver
    ):
        return []
    # +1: the prompt just recorded is the newest stored message; the caller appends it itself
    messages = await rec.rehydrate(
        user_id=user_id, session_id=session_id, n_turns=config.history_rehydrate_turns + 1
    )
    if messages and messages[-1].type == "human" and messages[-1].content == prompt:
        messages = messages[:-1]
    if messages:
        log.info("chat history: rehydrated %d messages session=%s", len(messages), session_id)
    return messages


async def end_turn(
    *,
    user_id: str,
    session_id: str,
    envelope: Mapping[str, Any],
    prompt: str | None = None,
    retrieved_results: Sequence[object] | None = None,
    recorder: ConversationRecorder | None = None,
    saver: BaseCheckpointSaver | None = None,
    trace: Sequence[object] | None = None,
    stream_trace_events: list[dict[str, Any]] | None = None,
) -> str | None:
    """Record the answer from a runtime response envelope (`text` + optional `ui`); with a `saver`, snapshot the
    thread's checkpoint in the background under the AI turn's sequence. With `trace` (the run's TraceEntry list),
    persist its REDACTED events under the AI turn (F6, B7/B9) — best-effort. Never raises."""
    message_id = uuid.uuid4().hex
    try:
        text = envelope.get("text") or ""
        ui = envelope.get("ui")
        sequence = await (recorder or get_recorder()).finish_turn(
            user_id=user_id,
            session_id=session_id,
            text=str(text),
            ui_surface=dict(ui) if isinstance(ui, Mapping) else None,
            citations=citations_from(retrieved_results),
            first_prompt=prompt,
            message_id=message_id,
        )
    except Exception as exc:  # noqa: BLE001
        log.error("chat history: could not record answer session=%s error_type=%s", session_id, type(exc).__name__)
        return None
    if saver is not None and isinstance(sequence, int):
        checkpointing.schedule_checkpoint_save(user_id=user_id, session_id=session_id, saver=saver, version=sequence)
    if stream_trace_events and isinstance(sequence, int):
        try:
            await asyncio.to_thread(get_trace_repository().save, user_id, session_id, sequence, message_id, stream_trace_events)
        except Exception as exc:
            log.error("chat history: could not record streamed trace session=%s turn=%s error_type=%s", session_id, sequence, type(exc).__name__)
    elif trace and isinstance(sequence, int):
        await save_trace(user_id=user_id, session_id=session_id, turn_seq=sequence, message_id=message_id, trace=trace)
    return message_id if isinstance(sequence, int) else None


@lru_cache(maxsize=1)
def get_trace_repository() -> TraceRepository:
    from shared.persistence.traces import TraceRepository

    return TraceRepository()


async def save_trace(
    *,
    user_id: str,
    session_id: str,
    turn_seq: int,
    message_id: str,
    trace: Sequence[object],
    repository: TraceRepository | None = None,
) -> bool:
    """Persist the redacted trace of one AI turn. Logs and returns False on any failure; never fails the turn."""
    from shared.streaming.trace_redaction import trace_events_from_entries

    try:
        events = trace_events_from_entries(trace)
        repo = repository or get_trace_repository()
        return await asyncio.to_thread(repo.save, user_id, session_id, turn_seq, message_id, events)
    except Exception as exc:  # noqa: BLE001
        log.error("chat history: could not record trace session=%s turn=%s error_type=%s", session_id, turn_seq, type(exc).__name__)
        return False


def describe_action(action: ActionRequest) -> str:
    """Human-readable text for a HITL answer, stored as the HUMAN turn."""
    value = action.value
    if value in (None, "", {}, []):
        return f"Selected: {action.action_id}"
    rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return f"Selected: {action.action_id} — {rendered}"


async def record_resume(
    *, user_id: str, session_id: str, action: ActionRequest, recorder: ConversationRecorder | None = None
) -> None:
    """Record a HITL answer as a HUMAN turn. Raises SessionNotFoundError for a session the caller doesn't own and
    OwnershipCheckError when the owner cannot be read (never fails open)."""
    try:
        await (recorder or get_recorder()).record_human_action(
            user_id=user_id, session_id=session_id, text=describe_action(action)
        )
    except (SessionNotFoundError, OwnershipCheckError):
        raise
    except Exception as exc:  # noqa: BLE001
        log.error("chat history: could not record HITL answer session=%s error_type=%s", session_id, type(exc).__name__)
