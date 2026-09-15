from __future__ import annotations

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, StateGraph
from langgraph.store.base import BaseStore

from analysis.nodes import analyze, compare, gather, report
from analysis.state import AnalysisState

_builder = StateGraph(AnalysisState)

_builder.add_node("gather", gather)
_builder.add_node("analyze", analyze)
_builder.add_node("compare", compare)
_builder.add_node("report", report)

_builder.set_entry_point("gather")
_builder.add_edge("gather", "analyze")
_builder.add_edge("analyze", "compare")
_builder.add_edge("compare", "report")
_builder.add_edge("report", END)


def compile_graph(
    *,
    checkpointer: BaseCheckpointSaver | None = None,
    store: BaseStore | None = None,
):
    return _builder.compile(checkpointer=checkpointer, store=store)
