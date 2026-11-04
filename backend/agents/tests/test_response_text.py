from __future__ import annotations

import asyncio

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import main
from shared.state import TraceEntry
from shared.response_text import final_ai_text


def test_final_ai_text_skips_trailing_tool_and_trace_messages() -> None:
    messages = [
        HumanMessage(content="Summarize the evidence."),
        AIMessage(content="", tool_calls=[{"name": "search", "args": {}, "id": "call-1"}]),
        ToolMessage(content="Retrieved 3 records.", tool_call_id="call-1"),
        AIMessage(content="The evidence supports the intervention."),
        ToolMessage(content="TRACE: response persisted", tool_call_id="call-1"),
    ]

    assert final_ai_text(messages) == "The evidence supports the intervention."


def test_final_ai_text_returns_none_without_an_assistant_answer() -> None:
    messages = [
        HumanMessage(content="Summarize the evidence."),
        AIMessage(content="", tool_calls=[{"name": "search", "args": {}, "id": "call-1"}]),
        ToolMessage(content="TRACE: search complete", tool_call_id="call-1"),
    ]

    assert final_ai_text(messages) is None


def test_final_ai_text_skips_empty_json_list_after_final_answer() -> None:
    messages = [
        HumanMessage(content="Summarize the evidence."),
        AIMessage(content="The evidence supports the intervention."),
        AIMessage(content="[]"),
    ]

    assert final_ai_text(messages) == "The evidence supports the intervention."


def test_final_ai_text_skips_tool_call_payload_after_final_answer() -> None:
    messages = [
        HumanMessage(content="Summarize the evidence."),
        AIMessage(content="The evidence supports the intervention."),
        AIMessage(content='{"name": "search", "args": {"query": "intervention"}}'),
    ]

    assert final_ai_text(messages) == "The evidence supports the intervention."


class _Context:
    session_id = "test-session"
    request_headers = {"Authorization": "Bearer test-token:test-user"}


class _CompletedGraph:
    def __init__(self, result: dict) -> None:
        self._result = result

    async def ainvoke(self, initial_state: dict, config: dict) -> dict:
        return self._result


def test_research_runtime_uses_final_ai_text_and_preserves_trace(monkeypatch) -> None:
    result = {
        "messages": [
            HumanMessage(content="Summarize the evidence."),
            AIMessage(content="The evidence supports the intervention."),
            ToolMessage(content="TRACE: response persisted", tool_call_id="call-1"),
        ],
        "trace": [TraceEntry(stage="SYNTHESIS", detail="complete")],
        "retrieved_results": [],
    }
    monkeypatch.setattr("research.graph.compile_graph", lambda **_: _CompletedGraph(result))

    response = asyncio.run(main.invoke({"prompt": "Summarize the evidence."}, _Context()))

    assert response["text"] == "The evidence supports the intervention."
    assert response["result"] == "The evidence supports the intervention.\n═══ AGENT TRACE ═══\n  [SYNTHESIS] complete\n  [TOTAL TOKENS] input=0 | output=0 | total=0\n═══════════════════"
    assert response["ui"]["version"] == "1.1"


def test_research_runtime_passes_response_document_to_presenter(monkeypatch) -> None:
    document = object()
    result = {
        "messages": [
            HumanMessage(content="Summarize the evidence."),
            AIMessage(content="The evidence supports the intervention."),
        ],
        "trace": [],
        "retrieved_results": [],
        "response_document": document,
    }
    monkeypatch.setattr("research.graph.compile_graph", lambda **_: _CompletedGraph(result))

    # The document is a stand-in: it only proves main.py forwards state's response_document.
    # Rendering an actual ResponseDocument is covered by the presenter's own tests.
    captured: dict = {}
    original_build_research_surface = main.build_research_surface

    def spy(*args, **kwargs):
        captured["document"] = kwargs.get("document")
        return original_build_research_surface(*args, **{**kwargs, "document": None})

    monkeypatch.setattr("main.build_research_surface", spy)

    response = asyncio.run(main.invoke({"prompt": "Summarize the evidence."}, _Context()))

    assert captured["document"] is document
    assert response["ui"]["version"] == "1.1"


def test_research_runtime_excludes_trace_without_final_ai_answer(monkeypatch) -> None:
    result = {
        "messages": [
            HumanMessage(content="Summarize the evidence."),
            ToolMessage(content="TRACE: search complete", tool_call_id="call-1"),
        ],
        "trace": [TraceEntry(stage="TOOL_CALL", detail="complete")],
        "retrieved_results": [],
    }
    monkeypatch.setattr("research.graph.compile_graph", lambda **_: _CompletedGraph(result))

    response = asyncio.run(main.invoke({"prompt": "Summarize the evidence."}, _Context()))

    assert response["text"] == "No response generated."
    assert response["result"] == "No response generated."