"""Callable entry point for the Analysis agent, used by the Pipeline Manager (B95)."""

from __future__ import annotations

from dataclasses import dataclass, field

from langchain_core.messages import HumanMessage

from analysis.graph import compile_graph
from analysis.state import AnalysisType
from shared.response_text import final_ai_text
from shared.schemas.response_blocks import ResponseDocument
from shared.state import TraceEntry
from shared.tools.base import DataSourceResult


@dataclass
class AnalysisOutcome:
    text: str
    document: ResponseDocument | None
    retrieved_results: list[DataSourceResult] = field(default_factory=list)
    trace: list[TraceEntry] = field(default_factory=list)


async def run_analysis(
    prompt: str,
    mode: str,
    user_id: str,
    session_id: str,
    *,
    analysis_type: str | None = None,
    scope: dict | None = None,
    long_term_context: list[str] | tuple[str, ...] = (),
) -> AnalysisOutcome:
    """Run the Analysis graph to completion with no checkpointer and no HITL pause.

    Called directly by the Pipeline Manager's in-process dispatcher (B95). ``scope``
    may give ``competitors``/``market``/``jurisdiction``/``focal_entity``; when it is
    ``None`` the ``scope`` node still extracts them from ``prompt``, but
    ``confirmed_scope=True`` means the graph never pauses on ``confirm_scope``.
    # ADR: user_id is the parent run's authenticated actor (B110) | a hardcoded placeholder here
    # would make shared.tools.attachments_kb.retrieve_attachments fail its id validation and
    # silently skip every pipeline-dispatched analysis step's attachments.
    """
    graph = compile_graph(checkpointer=None)
    scope_values = scope or {}

    initial_state = {
        "messages": [HumanMessage(content=prompt)],
        "mode": mode if mode in ("science", "business") else "science",
        "user_id": user_id,
        "session_id": session_id,
        "query": prompt,
        "analysis_type": AnalysisType(analysis_type) if analysis_type else None,
        "competitors": scope_values.get("competitors", []),
        "market": scope_values.get("market"),
        "jurisdiction": scope_values.get("jurisdiction"),
        "focal_entity": scope_values.get("focal_entity"),
        "confirmed_scope": True,
        "scope_revision": 0,
        "scope_correction": None,
        "retrieved_results": [],
        "findings": [],
        "comparisons": [],
        "report": "",
        "response_document": None,
        "trace": [],
        # The parent Pipeline run already recalled this; a sub-run never calls memory itself
        "long_term_context": list(long_term_context),
    }

    result = await graph.ainvoke(
        initial_state,
        config={"configurable": {"thread_id": session_id}},
    )

    text = final_ai_text(result.get("messages", [])) or "No response generated."
    return AnalysisOutcome(
        text=text,
        document=result.get("response_document"),
        retrieved_results=result.get("retrieved_results", []),
        trace=result.get("trace", []),
    )
