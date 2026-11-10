"""B72/F13: Research's clarifying question (triage -> clarify -> triage) and its resume path."""
from __future__ import annotations

import asyncio
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

import main
from research.nodes import build_clarify_surface, clarify, route_after_triage, triage
from research.state import ResearchState, TriagePlan
from shared.schemas.cei_ui import CEIUISurface


def _plan(*, selected=(), ambiguous=False) -> TriagePlan:
    return TriagePlan(selected_sources=list(selected), ambiguous=ambiguous, reasoning="test")


# ── routing ──────────────────────────────────────────────────────────────────


def test_route_after_triage_clarifies_on_zero_sources():
    assert route_after_triage({"triage_plan": _plan(selected=[])}) == "clarify"


def test_route_after_triage_clarifies_on_ambiguous_even_with_a_guess():
    from shared.schemas.citation import SourceType

    plan = _plan(selected=[SourceType.PUBMED], ambiguous=True)
    assert route_after_triage({"triage_plan": plan}) == "clarify"


def test_route_after_triage_retrieves_when_confident():
    from shared.schemas.citation import SourceType

    plan = _plan(selected=[SourceType.PUBMED], ambiguous=False)
    assert route_after_triage({"triage_plan": plan}) == "retrieve"


def test_route_after_triage_skips_clarify_once_already_clarified():
    """Only one clarify round per turn; also how subagent calls (drafting) bypass it."""
    assert route_after_triage({"triage_plan": _plan(selected=[]), "clarified": True}) == "retrieve"


def test_route_after_triage_handles_missing_plan():
    assert route_after_triage({}) == "retrieve"


# ── clarify surface + node ──────────────────────────────────────────────────


def test_build_clarify_surface_offers_sources_and_a_rephrase_field():
    surface = build_clarify_surface("ambiguous query", surface_id="research-clarify", revision=1)
    parsed = CEIUISurface.model_validate(surface)
    types = {c.type for c in parsed.components}
    assert "ChoicePicker" in types and "TextField" in types
    picker = next(c for c in parsed.components if c.type == "ChoicePicker")
    assert len(picker.options) > 1


def _clarify_graph():
    builder = StateGraph(ResearchState)
    builder.add_node("clarify", clarify)
    builder.add_edge(START, "clarify")
    builder.add_edge("clarify", END)
    return builder.compile(checkpointer=InMemorySaver())


def test_clarify_node_pauses_then_resumes_with_selected_source():
    graph = _clarify_graph()
    config = {"configurable": {"thread_id": "t1"}}
    paused = graph.invoke({"query": "GLP-1"}, config)
    surface = paused["__interrupt__"][0].value
    CEIUISurface.model_validate(surface)
    resumed = graph.invoke(
        Command(resume={"action_id": "select-clarify-source", "value": "pubmed"}), config
    )
    assert resumed["clarified"] is True
    assert "pubmed" in resumed["clarification_answer"]


def test_clarify_node_resumes_with_free_text_rephrase():
    graph = _clarify_graph()
    config = {"configurable": {"thread_id": "t2"}}
    graph.invoke({"query": "GLP-1"}, config)
    resumed = graph.invoke(
        Command(resume={"action_id": "submit-clarify-rephrase", "value": "GLP-1 trials in T2D"}), config
    )
    assert resumed["clarification_answer"] == "GLP-1 trials in T2D"


# ── triage incorporates the clarified answer, then resets it ───────────────


def test_triage_includes_clarification_in_prompt_and_resets_it(monkeypatch):
    raw = AIMessage(content="", usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})
    structured = Mock()
    structured.invoke.return_value = {"parsed": _plan(selected=[]), "raw": raw}
    model = Mock()
    model.with_structured_output.return_value = structured
    monkeypatch.setattr("research.nodes.get_model", lambda **_: model)

    result = triage({
        "messages": [HumanMessage(content="question")],
        "clarification_answer": "Use only this source: pubmed.",
    })

    user_prompt = structured.invoke.call_args.args[0][1]["content"]
    assert "User clarification: Use only this source: pubmed." in user_prompt
    assert result["clarification_answer"] is None


# ── end-to-end: triage -> clarify -> triage -> retrieve on one checkpointer ─


def test_research_graph_pauses_for_clarify_then_resumes_into_retrieve(monkeypatch):
    from shared.schemas.citation import SourceType

    raw = AIMessage(content="", usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})
    plans = [_plan(selected=[]), _plan(selected=[SourceType.PUBMED])]
    structured = Mock()
    structured.invoke.side_effect = [{"parsed": p, "raw": raw} for p in plans]
    model = Mock()
    model.with_structured_output.return_value = structured
    monkeypatch.setattr("research.nodes.get_model", lambda **_: model)

    pubmed = Mock(source_type=SourceType.PUBMED)
    from shared.tools.base import DataSourceResult
    pubmed.invoke.return_value = DataSourceResult(source_type=SourceType.PUBMED, results=[], citations=[])
    monkeypatch.setattr("research.nodes.ALL_DATA_SOURCE_TOOLS", [pubmed])

    # A partial graph (triage -> clarify/retrieve only): this test only needs to prove the
    # pause/resume loop, not the rest of the pipeline (which would need a 3rd mocked LLM call).
    from research.nodes import retrieve as retrieve_node

    builder = StateGraph(ResearchState)
    builder.add_node("triage", triage)
    builder.add_node("clarify", clarify)
    builder.add_node("retrieve", retrieve_node)
    builder.set_entry_point("triage")
    builder.add_conditional_edges("triage", route_after_triage, {"clarify": "clarify", "retrieve": "retrieve"})
    builder.add_edge("clarify", "triage")
    builder.add_edge("retrieve", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "t3"}}
    state = {
        "messages": [HumanMessage(content="ambiguous question")],
        "query": "ambiguous question",
        "clarified": False,
        "clarification_answer": None,
    }
    paused = graph.invoke(state, config)
    assert paused["__interrupt__"][0].value["surface_id"] == "research-clarify"

    resumed = graph.invoke(
        Command(resume={"action_id": "select-clarify-source", "value": "pubmed"}), config
    )
    assert resumed["triage_plan"].selected_sources == [SourceType.PUBMED]
    pubmed.invoke.assert_called_once()


# ── main.py op=resume (buffered envelope, local InMemorySaver path) ────────


class _Context:
    session_id = "0b0c4f7e-4444-4a44-8a44-000000000001"
    request_headers = {"Authorization": "Bearer test-token:test-user"}


class _CompletedGraph:
    def __init__(self, result: dict) -> None:
        self._result = result

    async def ainvoke(self, state, config):
        return self._result

    async def aget_state(self, config):
        return None


def test_main_resume_without_checkpoint_is_expired(monkeypatch):
    monkeypatch.setattr(main, "_persistent_research_checkpointer", lambda: None)
    payload = {
        "op": "resume",
        "action": {
            "request_id": "r1",
            "session_id": "0b0c4f7e-4444-4a44-8a44-000000000002",
            "surface_id": "research-clarify",
            "revision": 1,
            "interaction_id": "research-clarify-source",
            "component_id": "clarify-sources",
            "action_id": "select-clarify-source",
            "value": "pubmed",
        },
    }

    class _Context2:
        session_id = "0b0c4f7e-4444-4a44-8a44-000000000002"
        request_headers = {"Authorization": "Bearer test-token:test-user"}

    response = asyncio.run(main.invoke(payload, _Context2()))
    assert response["status"] == "rejected" and response["code"] == "expired"


def test_main_resume_completes_run(monkeypatch):
    monkeypatch.setattr(main, "_persistent_research_checkpointer", lambda: None)
    result = {
        "messages": [HumanMessage(content="ambiguous question"), AIMessage(content="Final answer.")],
        "trace": [],
        "retrieved_results": [],
        "response_document": None,
    }
    import research.graph as graph_module
    monkeypatch.setattr(graph_module, "compile_graph", lambda **_: _CompletedGraph(result))

    surface = build_clarify_surface("ambiguous question", surface_id="research-clarify", revision=1)
    sid = "0b0c4f7e-4444-4a44-8a44-000000000003"
    main._pending.register(sid, surface)

    class _Context3:
        session_id = sid
        request_headers = {"Authorization": "Bearer test-token:test-user"}

    gate = next(c for c in surface["components"] if c["type"] == "ChoicePicker")
    payload = {
        "op": "resume",
        "action": {
            "request_id": "r1",
            "session_id": sid,
            "surface_id": "research-clarify",
            "revision": 1,
            "interaction_id": gate["interaction_id"],
            "component_id": gate["id"],
            "action_id": "select-clarify-source",
            "value": "pubmed",
        },
    }
    response = asyncio.run(main.invoke(payload, _Context3()))
    assert response["status"] == "completed"
    assert response["text"] == "Final answer."
    assert response["action_response"]["status"] == "completed"


# ── main.py op=resume, AgentCoreMemorySaver (persistent) path ──────────────


def test_main_resume_persistent_checkpoint_missing_is_expired(monkeypatch):
    from unittest.mock import AsyncMock

    saver = Mock()
    saver.aget_tuple = AsyncMock(return_value=None)
    monkeypatch.setattr(main, "_persistent_research_checkpointer", lambda: saver)
    import research.graph as graph_module
    monkeypatch.setattr(graph_module, "compile_graph", lambda **_: _CompletedGraph({}))

    payload = {
        "op": "resume",
        "action": {
            "request_id": "r1",
            "session_id": "0b0c4f7e-4444-4a44-8a44-000000000004",
            "surface_id": "research-clarify",
            "revision": 1,
            "interaction_id": "research-clarify-source",
            "component_id": "clarify-sources",
            "action_id": "select-clarify-source",
            "value": "pubmed",
        },
    }

    class _Context4:
        session_id = "0b0c4f7e-4444-4a44-8a44-000000000004"
        request_headers = {"Authorization": "Bearer test-token:test-user"}

    response = asyncio.run(main.invoke(payload, _Context4()))
    assert response["status"] == "rejected" and response["code"] == "expired"
    saver.aget_tuple.assert_awaited_once()


# ── streaming path: a clarify pause ends the stream with hitl_request, not surface_end ──


def _decode(frame):
    import json

    fields = dict(line.split(": ", 1) for line in frame.decode().strip().splitlines())
    return int(fields["id"]), fields["event"], json.loads(fields["data"])


@pytest.mark.asyncio
async def test_stream_emits_hitl_request_instead_of_surface_end_on_clarify_pause(monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace

    class _Interrupt:
        def __init__(self, value):
            self.value = value

    clarify_surface = build_clarify_surface("ambiguous question", surface_id="research-clarify", revision=1)
    ambiguous_plan = _plan(selected=[])

    class Graph:
        async def astream(self, *_args, **_kwargs):
            yield "values", {"triage_plan": ambiguous_plan, "trace": [], "clarified": False}

        async def aget_state(self, _config):
            return SimpleNamespace(interrupts=[_Interrupt(clarify_surface)], values={})

    async def _end_turn(**_):
        return None

    monkeypatch.setattr(main, "gateway_request_context", lambda *_args: nullcontext())
    monkeypatch.setattr(main, "end_turn", _end_turn)
    session = SimpleNamespace(user_id="user", session_id="session")
    frames = [_decode(frame) async for frame in main._stream_research(Graph(), {}, {}, None, session, "q", None, True)]

    assert frames[-1][1] == "hitl_request"
    assert frames[-1][2]["surface_id"] == "research-clarify"
    assert not any(kind == "surface_end" for _, kind, _ in frames)
    assert not any(kind == "component_add" for _, kind, _ in frames)
