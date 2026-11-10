"""Shared resume-turn prefix for runtimes whose graphs pause via ``interrupt()`` (F13).

``analysis_main.py`` and ``pipeline_main.py`` each reimplemented this exact sequence:
parse the ``ActionRequest``, consult a replay fixture, re-register a cold thread's pending
surface from its checkpoint (or answer "expired"), then refuse a stale/mismatched action.
``drafting_main.py`` and ``main.py`` (Research) reuse it too. The two runtimes' behavior
diverges after this point (Pipeline creates a background job on ``approve-plan``; the
others resume the graph immediately), so callers still own ``record_resume``/touching the
thread LRU and invoking ``graph.ainvoke(Command(resume=...))`` themselves.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Mapping

from shared.runtime.checkpointing import restore_for_resume
from shared.runtime.ops import PendingInterrupts, check_resume, expired_envelope, parse_action
from shared.schemas.cei_ui import ActionRequest

RestoreFn = Callable[..., Awaitable[bool]]


async def resolve_resume_action(
    payload: Mapping[str, Any],
    *,
    session_id: str,
    user_id: str,
    checkpointer: Any,
    graph: Any,
    pending: PendingInterrupts,
    is_cold: bool,
    restore: RestoreFn = restore_for_resume,
    replay: Any | None = None,
    replay_key: str = "",
) -> tuple[ActionRequest, dict[str, Any] | None]:
    """Parse, replay-check, cold-restore-or-expire, then stale-check a resume action.

    Returns ``(action, None)`` when the caller should proceed with the resume, or
    ``(action, envelope)`` when the caller must return ``envelope`` immediately (a
    replay fixture, an expired checkpoint, or a stale/mismatched action). ``restore``
    is injectable: Research's ``AgentCoreMemorySaver`` path (B82) substitutes an
    ``aget_tuple``-based expiry check in place of the default ``restore_for_resume``
    (which restores a ChatHistoryStore snapshot into an in-process ``InMemorySaver``).
    """
    action = parse_action(payload)

    if replay is not None and replay.enabled:
        replayed = replay.on_resume(replay_key, session_id=session_id, action=action, job_id=None)
        if replayed is not None:
            return action, replayed

    if is_cold and pending.get(session_id) is None and action.session_id == session_id:
        # cold resume (runtime restart / eviction): bring back the interrupted graph + its pending surface
        if not await restore(
            user_id=user_id, session_id=session_id, saver=checkpointer, graph=graph, pending=pending
        ):
            return action, expired_envelope(action)  # B82: no checkpoint left to resume

    stale = check_resume(pending, session_id, action)
    if stale is not None:
        return action, stale

    return action, None
