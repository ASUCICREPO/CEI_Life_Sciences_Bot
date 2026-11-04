"""Short-term-memory checkpoints for the four AgentCore entrypoints (plan 3b4d4f64 decision 10).

After each turn the thread's latest LangGraph checkpoint is written, in the background, into the conversation's
own ChatHistoryStore partition (shared/persistence/checkpoints.py). When a thread is cold (runtime restart, LRU
eviction) the snapshot is restored into the in-process saver before the turn runs; if no usable snapshot exists the
caller falls back to text rehydration.

ADR: snapshot captured synchronously, written in a background task | Rationale: `get_tuple` on the in-process
InMemorySaver is cheap and must observe this turn's state before the next invocation mutates it; the DynamoDB writes
(gzip + chunks) must not delay the response | Alternative: synchronous write rejected (adds latency to every turn).
A per-session asyncio.Lock serializes overlapping writes for one session (e.g. a resume arriving while the
previous snapshot is still being written); versioned conditional writes make an older snapshot unable to replace a
newer one regardless of ordering. Nothing here raises into the turn.
"""
from __future__ import annotations

import asyncio
import logging
import weakref
from collections.abc import Mapping
from functools import lru_cache
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver

    from shared.persistence.checkpoints import CheckpointRepository
    from shared.runtime.ops import PendingInterrupts

log = logging.getLogger(__name__)

_background: set[asyncio.Task] = set()
_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()


@lru_cache(maxsize=1)
def get_checkpoint_repository() -> CheckpointRepository:
    """Process-wide repository (module-level instance, built on first use)."""
    from shared.persistence.checkpoints import CheckpointRepository

    return CheckpointRepository()


def _lock_for(session_id: str) -> asyncio.Lock:
    lock = _locks.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[session_id] = lock
    return lock


def _root_config(session_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": session_id, "checkpoint_ns": ""}}


async def restore_checkpoint(
    *, user_id: str, session_id: str, saver: BaseCheckpointSaver, repo: CheckpointRepository | None = None
) -> bool:
    """Load the caller's latest snapshot for `session_id` into `saver`. False (never raises) if none is usable."""
    try:
        repository = repo or get_checkpoint_repository()
        async with _lock_for(session_id):  # never read a snapshot that is mid-write in this process
            restored = await asyncio.to_thread(repository.restore, user_id, session_id, saver)
    except Exception:  # noqa: BLE001 — a cold start with text rehydration beats a failed turn
        log.exception("checkpoint: restore failed session=%s", session_id)
        return False
    if restored:
        log.info("checkpoint: restored short-term memory session=%s", session_id)
    return bool(restored)


def schedule_checkpoint_save(
    *,
    user_id: str,
    session_id: str,
    saver: BaseCheckpointSaver,
    version: int,
    repo: CheckpointRepository | None = None,
) -> asyncio.Task | None:
    """Snapshot the thread now and write it in the background under `version` (the AI turn's sequence)."""
    try:
        tup = saver.get_tuple(_root_config(session_id))
    except Exception:  # noqa: BLE001
        log.exception("checkpoint: could not read in-process checkpoint session=%s", session_id)
        return None
    if tup is None:
        return None

    async def _write() -> None:
        try:
            repository = repo or get_checkpoint_repository()
            async with _lock_for(session_id):
                await asyncio.to_thread(repository.save, user_id, session_id, tup, version)
        except Exception:  # noqa: BLE001 — logged, never surfaced to the user
            log.exception("checkpoint: background save failed session=%s v=%s", session_id, version)

    task = asyncio.create_task(_write())
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


async def drain_checkpoints() -> None:
    """Wait for background checkpoint writes (tests, graceful shutdown)."""
    if _background:
        await asyncio.gather(*list(_background), return_exceptions=True)


async def restore_for_resume(
    *,
    user_id: str,
    session_id: str,
    saver: BaseCheckpointSaver,
    graph: Any,
    pending: PendingInterrupts,
    repo: CheckpointRepository | None = None,
) -> bool:
    """Cold HITL resume: restore the snapshot and re-register the interrupt surface the thread is waiting on.

    Without this, a resume after a runtime restart would be answered `stale` because the in-process pending
    registry and the graph state were both lost.
    """
    if not await restore_checkpoint(user_id=user_id, session_id=session_id, saver=saver, repo=repo):
        return False
    try:
        from shared.runtime.ops import extract_interrupt_surface

        snapshot = await graph.aget_state({"configurable": {"thread_id": session_id}})
        interrupts = list(getattr(snapshot, "interrupts", None) or [])
        if not interrupts:
            for task in getattr(snapshot, "tasks", None) or []:
                interrupts.extend(getattr(task, "interrupts", None) or [])
        surface = extract_interrupt_surface({"__interrupt__": interrupts})
        if isinstance(surface, Mapping):
            pending.register(session_id, surface)
    except Exception:  # noqa: BLE001
        log.exception("checkpoint: could not re-register pending interrupt session=%s", session_id)
    return True
