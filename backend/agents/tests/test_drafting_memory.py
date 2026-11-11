"""Drafting uses actor-scoped long-term memory recall when explicitly configured (F11)."""
import asyncio
from unittest.mock import AsyncMock, Mock

import drafting_main
from drafting.state import DocumentType


def test_memory_load_uses_verified_actor_and_query(monkeypatch):
    client = Mock()
    client.retrieve_memory_records.return_value = {"memoryRecordSummaries": [
        {"content": {"text": "prefers APA"}, "namespaces": ["/users/user-a/"]}]}
    monkeypatch.setenv("CEI_DRAFTING_MEMORY_ID", "memory-id")
    monkeypatch.setenv("CEI_DRAFTING_LONG_TERM_ENABLED", "true")
    monkeypatch.setattr("boto3.client", lambda *_args, **_kwargs: client)
    assert asyncio.run(drafting_main._drafting_long_term_context("user-a", "citations")) == ["prefers APA"]
    assert client.retrieve_memory_records.call_args.kwargs["namespace"] == "/users/user-a/"


def test_missing_memory_configuration_never_creates_client(monkeypatch):
    monkeypatch.delenv("CEI_DRAFTING_MEMORY_ID", raising=False)
    monkeypatch.setenv("CEI_DRAFTING_LONG_TERM_ENABLED", "true")
    client = Mock()
    monkeypatch.setattr("boto3.client", client)
    assert asyncio.run(drafting_main._drafting_long_term_context("user-a", "query")) == []
    client.assert_not_called()


def test_memory_service_failure_degrades_to_no_context(monkeypatch):
    from botocore.exceptions import ClientError
    client = Mock()
    client.retrieve_memory_records.side_effect = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "RetrieveMemoryRecords")
    monkeypatch.setenv("CEI_DRAFTING_MEMORY_ID", "memory-id")
    monkeypatch.setenv("CEI_DRAFTING_LONG_TERM_ENABLED", "true")
    monkeypatch.setattr("boto3.client", lambda *_args, **_kwargs: client)
    assert asyncio.run(drafting_main._drafting_long_term_context("user-a", "query")) == []


def test_memory_retrieval_is_disabled_by_default(monkeypatch):
    monkeypatch.setenv("CEI_DRAFTING_MEMORY_ID", "memory-id")
    monkeypatch.delenv("CEI_DRAFTING_LONG_TERM_ENABLED", raising=False)
    client = Mock()
    monkeypatch.setattr("boto3.client", client)
    assert asyncio.run(drafting_main._drafting_long_term_context("user-a", "query")) == []
    client.assert_not_called()


class _Context:
    def __init__(self, session_id: str = "0b0c4f7e-8888-4a88-8a88-000000000001") -> None:
        self.session_id = session_id
        self.request_headers = {"Authorization": "Bearer test-token:test-user"}


class _CompletedGraph:
    async def ainvoke(self, state, config):
        from langchain_core.messages import AIMessage

        self.seen_state = state
        return {"messages": [AIMessage(content="Draft.")], "trace": [], "exports": []}


def test_drafting_passes_memory_to_new_run(monkeypatch) -> None:
    memory = AsyncMock(return_value=["prefers APA"])
    monkeypatch.setattr(drafting_main, "_drafting_long_term_context", memory)
    graph = _CompletedGraph()

    import drafting.graph as graph_module
    monkeypatch.setattr(graph_module, "compile_graph", lambda **_: graph)

    response = asyncio.run(drafting_main.invoke(
        {"prompt": "Write it", "document_type": DocumentType.GRANT.value}, _Context(),
    ))

    assert response["text"] == "Draft."
    assert graph.seen_state["long_term_context"] == ["prefers APA"]
    memory.assert_awaited_once_with("test-user", "Write it")


def test_drafting_resume_does_not_recall(monkeypatch) -> None:
    memory = AsyncMock(return_value=["prefers APA"])
    monkeypatch.setattr(drafting_main, "_drafting_long_term_context", memory)

    # A cold resume (no pending surface, no checkpoint) always answers "expired";
    # it must do so without ever touching long-term memory recall.
    session_id = "0b0c4f7e-8888-4a88-8a88-000000000002"
    payload = {
        "op": "resume",
        "action": {
            "request_id": "r1",
            "session_id": session_id,
            "surface_id": "drafting-outline",
            "revision": 1,
            "interaction_id": "drafting-outline-review",
            "component_id": "outline-review",
            "action_id": "approve-outline",
            "value": None,
        },
    }
    response = asyncio.run(drafting_main.invoke(payload, _Context(session_id)))
    assert response["status"] == "rejected" and response["code"] == "expired"
    memory.assert_not_called()


def test_research_subagent_forwards_parent_context_without_recalling(monkeypatch) -> None:
    """The in-process Research sub-run must receive the parent's context, not recall its own."""
    from drafting.nodes import research_subagent

    captured: dict = {}

    class _CapturingGraph:
        def batch(self, inputs, **_kwargs):
            captured["inputs"] = inputs
            return [{"messages": [], "trace": []} for _ in inputs]

    import research.graph as research_graph_module
    monkeypatch.setattr(research_graph_module, "compile_graph", lambda **_: _CapturingGraph())

    state = {
        "research_questions": ["What is the mechanism?"],
        "session_id": "session-a",
        "user_id": "user-a",
        "mode": "science",
        "long_term_context": ["prefers APA"],
    }
    research_subagent(state)

    assert captured["inputs"][0]["long_term_context"] == ["prefers APA"]
