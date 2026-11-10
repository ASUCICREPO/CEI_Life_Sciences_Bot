"""Research uses actor-scoped AgentCore checkpoints when explicitly configured."""
import asyncio
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import main


def test_memory_load_uses_verified_actor_and_query(monkeypatch):
    client = Mock()
    client.retrieve_memory_records.return_value = {"memoryRecordSummaries": [
        {"content": {"text": "prefers APA"}, "namespaces": ["/users/user-a/"]}]}
    monkeypatch.setenv("CEI_RESEARCH_MEMORY_ID", "memory-id")
    monkeypatch.setenv("CEI_RESEARCH_LONG_TERM_ENABLED", "true")
    monkeypatch.setattr("boto3.client", lambda *_args, **_kwargs: client)
    assert asyncio.run(main._research_long_term_context("user-a", "citations")) == ["prefers APA"]
    assert client.retrieve_memory_records.call_args.kwargs["namespace"] == "/users/user-a/"


def test_missing_memory_configuration_never_creates_client(monkeypatch):
    monkeypatch.delenv("CEI_RESEARCH_MEMORY_ID", raising=False)
    monkeypatch.setenv("CEI_RESEARCH_LONG_TERM_ENABLED", "true")
    client = Mock()
    monkeypatch.setattr("boto3.client", client)
    assert asyncio.run(main._research_long_term_context("user-a", "query")) == []
    client.assert_not_called()


def test_memory_service_failure_degrades_to_no_context(monkeypatch):
    from botocore.exceptions import ClientError
    client = Mock()
    client.retrieve_memory_records.side_effect = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "RetrieveMemoryRecords")
    monkeypatch.setenv("CEI_RESEARCH_MEMORY_ID", "memory-id")
    monkeypatch.setenv("CEI_RESEARCH_LONG_TERM_ENABLED", "true")
    monkeypatch.setattr("boto3.client", lambda *_args, **_kwargs: client)
    assert asyncio.run(main._research_long_term_context("user-a", "query")) == []


def test_foreign_namespace_record_still_fails_the_turn(monkeypatch):
    client = Mock()
    client.retrieve_memory_records.return_value = {"memoryRecordSummaries": [
        {"content": {"text": "other user"}, "namespaces": ["/users/user-b/"]}]}
    monkeypatch.setenv("CEI_RESEARCH_MEMORY_ID", "memory-id")
    monkeypatch.setenv("CEI_RESEARCH_LONG_TERM_ENABLED", "true")
    monkeypatch.setattr("boto3.client", lambda *_args, **_kwargs: client)
    with pytest.raises(ValueError, match="namespace"):
        asyncio.run(main._research_long_term_context("user-a", "query"))


def test_memory_retrieval_is_disabled_by_default(monkeypatch):
    monkeypatch.setenv("CEI_RESEARCH_MEMORY_ID", "memory-id")
    monkeypatch.delenv("CEI_RESEARCH_LONG_TERM_ENABLED", raising=False)
    client = Mock()
    monkeypatch.setattr("boto3.client", client)
    assert asyncio.run(main._research_long_term_context("user-a", "query")) == []
    client.assert_not_called()


def test_research_passes_memory_to_nonstream_graph(monkeypatch):
    from langchain_core.messages import AIMessage
    memory = AsyncMock(return_value=["prefers APA"])
    monkeypatch.setattr(main, "_research_long_term_context", memory)
    seen = {}

    class Graph:
        async def ainvoke(self, state, config):
            seen.update(state)
            return {"messages": [AIMessage(content="done")], "trace": [], "retrieved_results": []}

    monkeypatch.setattr("research.graph.compile_graph", lambda **_: Graph())
    context = SimpleNamespace(session_id="0b0c4f7e-3333-4a33-8a33-000000000001",
                              request_headers={"Authorization": "Bearer test-token:test-user"})
    result = asyncio.run(main.invoke({"prompt": "question"}, context))
    assert result["text"] == "done" and seen["long_term_context"] == ["prefers APA"]
    memory.assert_awaited_once_with("test-user", "question")


@pytest.mark.asyncio
async def test_research_passes_memory_to_stream_graph(monkeypatch):
    seen = {}
    monkeypatch.setattr(main, "gateway_request_context", lambda *_: nullcontext())
    monkeypatch.setattr(main, "_research_long_term_context", AsyncMock(return_value=["prefers APA"]))
    monkeypatch.setattr(main, "_research_checkpoint_context", AsyncMock(return_value=(None, True, True)))
    monkeypatch.setattr(main, "begin_turn", AsyncMock(return_value=[]))
    monkeypatch.setattr("research.graph.compile_graph", lambda **_: object())

    async def stream(_graph, initial, *_args):
        seen.update(initial)
        yield b"stream"

    monkeypatch.setattr(main, "_stream_research", stream)
    session = SimpleNamespace(user_id="user-a", session_id="session-a")
    frames = [frame async for frame in main._begin_stream_research(None, session, "question", "science", "model")]
    assert frames == [b"stream"] and seen["long_term_context"] == ["prefers APA"]
    main._research_long_term_context.assert_awaited_once_with("user-a", "question")


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
