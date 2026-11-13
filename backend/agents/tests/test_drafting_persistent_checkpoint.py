"""Drafting uses an actor-scoped AgentCoreMemorySaver checkpoint when explicitly configured.

Mirrors tests/test_research_memory.py's coverage of main.py's equivalent gate/context functions,
plus the turn-level wiring (`begin_turn`'s saver, and which carry-forward loader a cold vs. warm
persistent turn uses).
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from langchain_core.messages import AIMessage

import drafting_main
from drafting.state import DocumentType


@pytest.fixture(autouse=True)
def _clear_persistent_checkpointer_cache():
    """The gate is `lru_cache`d process-wide; never let one test's env/monkeypatch leak into another.

    A test that monkeypatches `_persistent_drafting_checkpointer` itself (replacing it with a plain
    lambda) loses `cache_clear` until `monkeypatch` restores the real function at its own teardown;
    guard both calls so this fixture's teardown never races that restore.
    """
    if hasattr(drafting_main._persistent_drafting_checkpointer, "cache_clear"):
        drafting_main._persistent_drafting_checkpointer.cache_clear()
    yield
    if hasattr(drafting_main._persistent_drafting_checkpointer, "cache_clear"):
        drafting_main._persistent_drafting_checkpointer.cache_clear()


class _Context:
    def __init__(self, session_id: str = "0b0c4f7e-9999-4a99-8a99-000000000001") -> None:
        self.session_id = session_id
        self.request_headers = {"Authorization": "Bearer test-token:test-user"}


# --- the gate: a configured Memory ID switches the saver (no separate flag) ---------------------


def test_no_memory_id_keeps_in_memory_saver(monkeypatch):
    monkeypatch.delenv("CEI_DRAFTING_MEMORY_ID", raising=False)
    created = Mock()
    monkeypatch.setattr("shared.memory.agentcore.create_checkpointer", created)
    assert drafting_main._persistent_drafting_checkpointer() is None
    created.assert_not_called()


def test_memory_id_creates_the_persistent_saver(monkeypatch):
    monkeypatch.setenv("CEI_DRAFTING_MEMORY_ID", "memory-id")
    saver = Mock()
    created = Mock(return_value=saver)
    monkeypatch.setattr("shared.memory.agentcore.create_checkpointer", created)
    assert drafting_main._persistent_drafting_checkpointer() is saver
    created.assert_called_once_with("memory-id")


# --- _drafting_checkpoint_context: warm/persistent detection via aget_tuple -----------------------


def test_existing_persistent_checkpoint_is_used_without_rehydration(monkeypatch):
    saver = Mock()
    saver.aget_tuple = AsyncMock(return_value=object())
    monkeypatch.setattr(drafting_main, "_persistent_drafting_checkpointer", lambda: saver)
    actual, warm, persistent = asyncio.run(drafting_main._drafting_checkpoint_context("user-a", "session-a"))
    assert (actual, warm, persistent) == (saver, True, True)
    saver.aget_tuple.assert_awaited_once_with({"configurable": {"thread_id": "session-a", "actor_id": "user-a"}})


def test_new_persistent_session_rehydrates_history(monkeypatch):
    saver = Mock()
    saver.aget_tuple = AsyncMock(return_value=None)
    monkeypatch.setattr(drafting_main, "_persistent_drafting_checkpointer", lambda: saver)
    assert asyncio.run(drafting_main._drafting_checkpoint_context("user-a", "session-b")) == (saver, False, True)


def test_persistent_memory_failure_does_not_silently_fallback(monkeypatch):
    saver = Mock()
    saver.aget_tuple = AsyncMock(side_effect=RuntimeError("memory unavailable"))
    monkeypatch.setattr(drafting_main, "_persistent_drafting_checkpointer", lambda: saver)
    with pytest.raises(RuntimeError, match="memory unavailable"):
        asyncio.run(drafting_main._drafting_checkpoint_context("user-a", "session-a"))


def test_local_mode_keeps_in_memory_saver(monkeypatch):
    monkeypatch.setattr(drafting_main, "_persistent_drafting_checkpointer", lambda: None)
    monkeypatch.setattr(drafting_main, "_thread_ids", {"existing": True})
    assert asyncio.run(drafting_main._drafting_checkpoint_context("user-a", "existing")) == (
        drafting_main._checkpointer, True, False,
    )


# --- turn wiring: begin_turn's saver, and which carry-forward loader runs ------------------------


class _CompletedGraph:
    def __init__(self, state_values: dict | None = None) -> None:
        self._state_values = state_values or {}

    async def ainvoke(self, state, config):
        self.seen_state = state
        self.seen_config = config
        return {"messages": [AIMessage(content="Draft.")], "trace": [], "exports": []}

    async def aget_state(self, config):
        from types import SimpleNamespace

        return SimpleNamespace(values=self._state_values)


def test_first_persistent_turn_has_no_checkpoint_and_uses_cold_recovery(monkeypatch):
    """No existing checkpoint (warm=False): begin_turn gets no saver, and the cold `md`-export
    recovery runs instead of the warm `aget_state` carry-forward."""
    saver = Mock()
    monkeypatch.setattr(drafting_main, "_drafting_checkpoint_context", AsyncMock(return_value=(saver, False, True)))

    seen_begin_turn: dict = {}

    async def _begin_turn(**kwargs):
        seen_begin_turn.update(kwargs)
        return []

    cold_carry_forward = AsyncMock(return_value={"draft": "from md export"})
    warm_carry_forward = AsyncMock(return_value={"draft": "from checkpoint"})
    monkeypatch.setattr(drafting_main, "begin_turn", _begin_turn)
    monkeypatch.setattr(drafting_main, "end_turn", AsyncMock(return_value=None))
    monkeypatch.setattr(drafting_main, "_load_cold_carry_forward", cold_carry_forward)
    monkeypatch.setattr(drafting_main, "_load_warm_carry_forward", warm_carry_forward)

    graph = _CompletedGraph()
    import drafting.graph as graph_module
    monkeypatch.setattr(graph_module, "compile_graph", lambda **_: graph)

    response = asyncio.run(drafting_main.invoke(
        {"prompt": "Write it", "document_type": DocumentType.GRANT.value}, _Context(),
    ))

    assert response["text"] == "Draft."
    assert seen_begin_turn["saver"] is None  # persistent: begin_turn never touches the saver itself
    assert seen_begin_turn["warm"] is False
    cold_carry_forward.assert_awaited_once()
    warm_carry_forward.assert_not_called()
    assert graph.seen_state["draft"] == "from md export"
    # actor-scoped config, even though the persistent saver already carries actor_id internally
    assert graph.seen_config == {"configurable": {"thread_id": _Context().session_id, "actor_id": "test-user"}}


def test_existing_persistent_checkpoint_uses_warm_carry_forward(monkeypatch):
    """An existing checkpoint (warm=True): carry-forward comes from `aget_state`, not the md export."""
    saver = Mock()
    monkeypatch.setattr(drafting_main, "_drafting_checkpoint_context", AsyncMock(return_value=(saver, True, True)))

    seen_begin_turn: dict = {}

    async def _begin_turn(**kwargs):
        seen_begin_turn.update(kwargs)
        return []

    cold_carry_forward = AsyncMock(return_value={"draft": "from md export"})
    warm_carry_forward = AsyncMock(return_value={"draft": "from checkpoint"})
    monkeypatch.setattr(drafting_main, "begin_turn", _begin_turn)
    monkeypatch.setattr(drafting_main, "end_turn", AsyncMock(return_value=None))
    monkeypatch.setattr(drafting_main, "_load_cold_carry_forward", cold_carry_forward)
    monkeypatch.setattr(drafting_main, "_load_warm_carry_forward", warm_carry_forward)

    graph = _CompletedGraph()
    import drafting.graph as graph_module
    monkeypatch.setattr(graph_module, "compile_graph", lambda **_: graph)

    response = asyncio.run(drafting_main.invoke(
        {"prompt": "Write it", "document_type": DocumentType.GRANT.value}, _Context(),
    ))

    assert response["text"] == "Draft."
    assert seen_begin_turn["saver"] is None
    assert seen_begin_turn["warm"] is True
    warm_carry_forward.assert_awaited_once()
    cold_carry_forward.assert_not_called()
    assert graph.seen_state["draft"] == "from checkpoint"


def test_end_turn_gets_no_saver_when_persistent(monkeypatch):
    """`end_turn`'s own background checkpoint snapshot is Research/Drafting's legacy ChatHistoryStore
    path; a persistent run must not schedule it, since AgentCore Memory already wrote the checkpoint."""
    saver = Mock()
    monkeypatch.setattr(drafting_main, "_drafting_checkpoint_context", AsyncMock(return_value=(saver, True, True)))
    monkeypatch.setattr(drafting_main, "begin_turn", AsyncMock(return_value=[]))
    monkeypatch.setattr(drafting_main, "_load_warm_carry_forward", AsyncMock(return_value={}))

    end_turn = AsyncMock(return_value=None)
    monkeypatch.setattr(drafting_main, "end_turn", end_turn)

    graph = _CompletedGraph()
    import drafting.graph as graph_module
    monkeypatch.setattr(graph_module, "compile_graph", lambda **_: graph)

    asyncio.run(drafting_main.invoke({"prompt": "Write it"}, _Context()))

    end_turn.assert_awaited_once()
    assert end_turn.call_args.kwargs["saver"] is None
