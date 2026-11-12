from __future__ import annotations

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, StateGraph
from langgraph.store.base import BaseStore

from drafting.nodes import (
    approve_outline,
    chat_answer,
    draft,
    export_documents,
    outline,
    plan_research,
    plan_visuals,
    research_guidelines,
    research_subagent,
    route_after_outline_approval,
    route_after_request,
    route_request,
    revise,
    should_research,
    should_revise,
)
from drafting.state import DraftingState

_builder = StateGraph(DraftingState)

_builder.add_node("route_request", route_request)
_builder.add_node("research_guidelines", research_guidelines)
_builder.add_node("plan_research", plan_research)
_builder.add_node("research_subagent", research_subagent)
_builder.add_node("outline", outline)
_builder.add_node("approve_outline", approve_outline)
_builder.add_node("draft", draft)
_builder.add_node("revise", revise)
_builder.add_node("plan_visuals", plan_visuals)
_builder.add_node("export_documents", export_documents)
_builder.add_node("chat_answer", chat_answer)

_builder.set_entry_point("route_request")
_builder.add_conditional_edges(
    "route_request",
    route_after_request,
    {
        "research": "research_guidelines",
        "outline": "outline",
        "draft": "draft",
        "revise": "revise",
        "export": "export_documents",
        "chat": "chat_answer",
    },
)
_builder.add_edge("research_guidelines", "plan_research")
_builder.add_conditional_edges(
    "plan_research", should_research, {"research": "research_subagent", "draft": "outline"}
)
_builder.add_edge("research_subagent", "outline")
_builder.add_edge("outline", "approve_outline")
_builder.add_conditional_edges(
    "approve_outline", route_after_outline_approval, {"draft": "draft", "outline": "outline", "end": END}
)
_builder.add_conditional_edges("draft", should_revise, {"revise": "revise", "end": "plan_visuals"})
_builder.add_conditional_edges("revise", should_revise, {"revise": "revise", "end": "plan_visuals"})
_builder.add_edge("plan_visuals", "export_documents")
_builder.add_edge("export_documents", END)
_builder.add_edge("chat_answer", END)


def compile_graph(
    *,
    checkpointer: BaseCheckpointSaver | None = None,
    store: BaseStore | None = None,
):
    return _builder.compile(checkpointer=checkpointer, store=store)
