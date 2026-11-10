"""In-process Pipeline sub-runs have no user or checkpointer, so B72 pauses must not fire there."""
from __future__ import annotations

import asyncio

from langchain_core.messages import HumanMessage

import drafting.graph
import research.graph
from drafting.nodes import approve_outline
from drafting.state import DocumentType
from pipeline_manager.dispatcher import InProcessDispatcher


class _CapturingGraph:
    def __init__(self) -> None:
        self.state: dict | None = None

    async def ainvoke(self, state, config=None):
        self.state = state
        return {"messages": [], "trace": []}


def test_approve_outline_skips_pause_when_non_interactive() -> None:
    # interrupt() outside a running graph raises, so returning proves no pause was attempted
    result = approve_outline({
        "messages": [HumanMessage(content="x")],
        "document_type": DocumentType.GRANT,
        "outline": "1. Aims",
        "outline_revision": 1,
        "skip_outline_approval": True,
    })
    assert result["outline_approved"] is True
    assert result["trace"][0].detail == "skipped: non-interactive"


def test_dispatcher_drafting_step_skips_outline_approval(monkeypatch) -> None:
    graph = _CapturingGraph()
    monkeypatch.setattr(drafting.graph, "compile_graph", lambda *a, **k: graph)
    asyncio.run(InProcessDispatcher()._run_drafting("p", "science", "u1", "s1:step:a", "a", "grant"))
    assert graph.state["skip_outline_approval"] is True


def test_dispatcher_research_step_skips_clarify(monkeypatch) -> None:
    graph = _CapturingGraph()
    monkeypatch.setattr(research.graph, "compile_graph", lambda *a, **k: graph)
    asyncio.run(InProcessDispatcher()._run_research("p", "science", "u1", "s1:step:a", "a"))
    assert graph.state["clarified"] is True
