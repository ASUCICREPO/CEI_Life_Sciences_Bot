"""Research uses actor-scoped AgentCore checkpoints when explicitly configured."""
import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

import main


def test_existing_persistent_checkpoint_is_used_without_rehydration(monkeypatch):
    saver = Mock()
    saver.aget_tuple = AsyncMock(return_value=object())
    monkeypatch.setattr(main, "_persistent_research_checkpointer", lambda: saver)
    actual, warm, persistent = asyncio.run(main._research_checkpoint_context("user-a", "session-a"))
    assert (actual, warm, persistent) == (saver, True, True)
    saver.aget_tuple.assert_awaited_once_with({"configurable": {"thread_id": "session-a", "actor_id": "user-a"}})


def test_new_persistent_session_rehydrates_history(monkeypatch):
    saver = Mock()
    saver.aget_tuple = AsyncMock(return_value=None)
    monkeypatch.setattr(main, "_persistent_research_checkpointer", lambda: saver)
    assert asyncio.run(main._research_checkpoint_context("user-a", "session-b")) == (saver, False, True)


def test_persistent_memory_failure_does_not_silently_fallback(monkeypatch):
    saver = Mock()
    saver.aget_tuple = AsyncMock(side_effect=RuntimeError("memory unavailable"))
    monkeypatch.setattr(main, "_persistent_research_checkpointer", lambda: saver)
    with pytest.raises(RuntimeError, match="memory unavailable"):
        asyncio.run(main._research_checkpoint_context("user-a", "session-a"))


def test_local_mode_keeps_in_memory_saver(monkeypatch):
    monkeypatch.setattr(main, "_persistent_research_checkpointer", lambda: None)
    monkeypatch.setattr(main, "_thread_ids", {"existing": True})
    assert asyncio.run(main._research_checkpoint_context("user-a", "existing")) == (main._checkpointer, True, False)
