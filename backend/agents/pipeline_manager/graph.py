from __future__ import annotations

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, StateGraph
from langgraph.store.base import BaseStore

from pipeline_manager.nodes import (
    approve,
    dispatch,
    monitor,
    plan,
    should_continue,
    synthesize,
)
from pipeline_manager.state import PipelineState

_builder = StateGraph(PipelineState)

_builder.add_node("plan", plan)
_builder.add_node("approve", approve)
_builder.add_node("dispatch", dispatch)
_builder.add_node("monitor", monitor)
_builder.add_node("synthesize", synthesize)

_builder.set_entry_point("plan")
_builder.add_edge("plan", "approve")
_builder.add_conditional_edges(
    "approve",
    lambda s: "dispatch" if s.get("plan_approved") else "end",
    {"dispatch": "dispatch", "end": END},
)
_builder.add_conditional_edges(
    "dispatch",
    should_continue,
    {"monitor": "monitor", "dispatch": "dispatch", "synthesize": "synthesize"},
)
_builder.add_conditional_edges(
    "monitor",
    should_continue,
    {"monitor": "monitor", "dispatch": "dispatch", "synthesize": "synthesize"},
)
_builder.add_edge("synthesize", END)


def compile_graph(
    *,
    checkpointer: BaseCheckpointSaver | None = None,
    store: BaseStore | None = None,
):
    return _builder.compile(checkpointer=checkpointer, store=store)
