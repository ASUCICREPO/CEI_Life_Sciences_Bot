from __future__ import annotations

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, StateGraph
from langgraph.store.base import BaseStore

from analysis.nodes import analyze, compare, confirm_scope, gather, report, scope
from analysis.state import AnalysisState

_builder = StateGraph(AnalysisState)

_builder.add_node("scope", scope)
_builder.add_node("confirm_scope", confirm_scope)
_builder.add_node("gather", gather)
_builder.add_node("analyze", analyze)
_builder.add_node("compare", compare)
_builder.add_node("report", report)

_builder.set_entry_point("scope")
_builder.add_edge("scope", "confirm_scope")


def _route_after_confirm(state: AnalysisState) -> str:
    if state.get("confirmed_scope"):
        return "gather"
    if state.get("scope_correction"):
        return "scope"
    return END


_builder.add_conditional_edges(
    "confirm_scope",
    _route_after_confirm,
    {"gather": "gather", "scope": "scope", END: END},
)
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
