"""Pipeline Manager uses actor-scoped long-term memory recall when explicitly configured (F11)."""
import asyncio
from unittest.mock import AsyncMock, Mock

from langchain_core.messages import AIMessage

import pipeline_main
from pipeline_manager.dispatcher import InProcessDispatcher
from pipeline_manager.state import PipelinePlan, PipelineStep


def test_memory_load_uses_verified_actor_and_query(monkeypatch):
    client = Mock()
    client.retrieve_memory_records.return_value = {"memoryRecordSummaries": [
        {"content": {"text": "prefers APA"}, "namespaces": ["/users/user-a/"]}]}
    monkeypatch.setenv("CEI_PIPELINE_MEMORY_ID", "memory-id")
    monkeypatch.setenv("CEI_PIPELINE_LONG_TERM_ENABLED", "true")
    monkeypatch.setattr("boto3.client", lambda *_args, **_kwargs: client)
    assert asyncio.run(pipeline_main._pipeline_long_term_context("user-a", "citations")) == ["prefers APA"]
    assert client.retrieve_memory_records.call_args.kwargs["namespace"] == "/users/user-a/"


def test_missing_memory_configuration_never_creates_client(monkeypatch):
    monkeypatch.delenv("CEI_PIPELINE_MEMORY_ID", raising=False)
    monkeypatch.setenv("CEI_PIPELINE_LONG_TERM_ENABLED", "true")
    client = Mock()
    monkeypatch.setattr("boto3.client", client)
    assert asyncio.run(pipeline_main._pipeline_long_term_context("user-a", "query")) == []
    client.assert_not_called()


def test_memory_service_failure_degrades_to_no_context(monkeypatch):
    from botocore.exceptions import ClientError
    client = Mock()
    client.retrieve_memory_records.side_effect = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "RetrieveMemoryRecords")
    monkeypatch.setenv("CEI_PIPELINE_MEMORY_ID", "memory-id")
    monkeypatch.setenv("CEI_PIPELINE_LONG_TERM_ENABLED", "true")
    monkeypatch.setattr("boto3.client", lambda *_args, **_kwargs: client)
    assert asyncio.run(pipeline_main._pipeline_long_term_context("user-a", "query")) == []


def test_memory_retrieval_is_disabled_by_default(monkeypatch):
    monkeypatch.setenv("CEI_PIPELINE_MEMORY_ID", "memory-id")
    monkeypatch.delenv("CEI_PIPELINE_LONG_TERM_ENABLED", raising=False)
    client = Mock()
    monkeypatch.setattr("boto3.client", client)
    assert asyncio.run(pipeline_main._pipeline_long_term_context("user-a", "query")) == []
    client.assert_not_called()


class _Context:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.request_headers = {"Authorization": "Bearer test-token:test-user"}


class _CompletedGraph:
    async def ainvoke(self, state, config):
        self.seen_state = state
        return {"messages": [AIMessage(content="Done.")], "trace": [], "retrieved_results": [], "plan": None}


def test_pipeline_passes_memory_to_new_run(monkeypatch) -> None:
    memory = AsyncMock(return_value=["prefers APA"])
    monkeypatch.setattr(pipeline_main, "_pipeline_long_term_context", memory)
    graph = _CompletedGraph()
    monkeypatch.setattr(pipeline_main, "_compile_graph", lambda: graph)

    session_id = "0b0c4f7e-7777-4a77-8a77-000000000001"
    response = asyncio.run(pipeline_main.invoke({"prompt": "Research and draft"}, _Context(session_id)))

    assert response["status"] == "completed"
    assert graph.seen_state["long_term_context"] == ["prefers APA"]
    memory.assert_awaited_once_with("test-user", "Research and draft")


def test_pipeline_resume_does_not_recall(monkeypatch) -> None:
    memory = AsyncMock(return_value=["prefers APA"])
    monkeypatch.setattr(pipeline_main, "_pipeline_long_term_context", memory)

    # A cold resume (no pending surface, no checkpoint) always answers "expired";
    # it must do so without ever touching long-term memory recall. A resumed background
    # job reuses the long_term_context already captured in the checkpointed graph state.
    session_id = "0b0c4f7e-7777-4a77-8a77-000000000002"
    payload = {
        "op": "resume",
        "action": {
            "request_id": "r1",
            "session_id": session_id,
            "surface_id": "pipeline-plan",
            "revision": 1,
            "interaction_id": "pipeline-plan-review",
            "component_id": "plan-review",
            "action_id": "approve-plan",
            "value": None,
        },
    }
    response = asyncio.run(pipeline_main.invoke(payload, _Context(session_id)))
    assert response["status"] == "rejected" and response["code"] == "expired"
    memory.assert_not_called()


def _step(step_id: str, agent: str) -> PipelineStep:
    return PipelineStep(step_id=step_id, agent=agent, description="do it", dependencies=[])


def test_dispatcher_forwards_parent_context_to_research_step(monkeypatch) -> None:
    """An in-process sub-run has no authenticated caller of its own; it must not recall."""
    captured: dict = {}

    class _CapturingGraph:
        async def ainvoke(self, state, config):
            captured["state"] = state
            return {"messages": [], "trace": []}

    import research.graph as research_graph_module
    monkeypatch.setattr(research_graph_module, "compile_graph", lambda **_: _CapturingGraph())

    asyncio.run(InProcessDispatcher().run(
        _step("a", "research"), session_id="s1", mode="science", user_id="u1",
        dependency_outputs={}, long_term_context=["prefers APA"],
    ))
    assert captured["state"]["long_term_context"] == ["prefers APA"]


def test_dispatcher_forwards_parent_context_to_drafting_step(monkeypatch) -> None:
    captured: dict = {}

    class _CapturingGraph:
        async def ainvoke(self, state, config):
            captured["state"] = state
            return {"messages": [], "trace": []}

    import drafting.graph as drafting_graph_module
    monkeypatch.setattr(drafting_graph_module, "compile_graph", lambda **_: _CapturingGraph())

    asyncio.run(InProcessDispatcher().run(
        _step("a", "drafting"), session_id="s1", mode="science", user_id="u1",
        dependency_outputs={}, long_term_context=["prefers APA"],
    ))
    assert captured["state"]["long_term_context"] == ["prefers APA"]


def test_dispatcher_defaults_to_no_context_when_not_given(monkeypatch) -> None:
    captured: dict = {}

    class _CapturingGraph:
        async def ainvoke(self, state, config):
            captured["state"] = state
            return {"messages": [], "trace": []}

    import research.graph as research_graph_module
    monkeypatch.setattr(research_graph_module, "compile_graph", lambda **_: _CapturingGraph())

    asyncio.run(InProcessDispatcher().run(
        _step("a", "research"), session_id="s1", mode="science", user_id="u1", dependency_outputs={},
    ))
    assert captured["state"]["long_term_context"] == []
