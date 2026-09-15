from __future__ import annotations

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, StateGraph
from langgraph.store.base import BaseStore

from drafting.nodes import draft, research_guidelines, revise, should_revise
from drafting.state import DraftingState

_builder = StateGraph(DraftingState)

_builder.add_node("research_guidelines", research_guidelines)
_builder.add_node("draft", draft)
_builder.add_node("revise", revise)

_builder.set_entry_point("research_guidelines")
_builder.add_edge("research_guidelines", "draft")
_builder.add_conditional_edges("draft", should_revise, {"revise": "revise", "end": END})
_builder.add_conditional_edges("revise", should_revise, {"revise": "revise", "end": END})


def compile_graph(
    *,
    checkpointer: BaseCheckpointSaver | None = None,
    store: BaseStore | None = None,
):
    return _builder.compile(checkpointer=checkpointer, store=store)
