from __future__ import annotations

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, StateGraph
from langgraph.store.base import BaseStore

from research.nodes import cite, retrieve, synthesize, triage
from research.state import ResearchState

_builder = StateGraph(ResearchState)

_builder.add_node("triage", triage)
_builder.add_node("retrieve", retrieve)
_builder.add_node("synthesize", synthesize)
_builder.add_node("cite", cite)

_builder.set_entry_point("triage")
_builder.add_edge("triage", "retrieve")
_builder.add_edge("retrieve", "synthesize")
_builder.add_edge("synthesize", "cite")
_builder.add_edge("cite", END)


def compile_graph(
    *,
    checkpointer: BaseCheckpointSaver | None = None,
    store: BaseStore | None = None,
):
    return _builder.compile(checkpointer=checkpointer, store=store)
