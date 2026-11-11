"""B72/F13: Drafting's outline-approval HITL point (outline -> approve_outline -> draft) and its resume path."""
from __future__ import annotations

import asyncio
from unittest.mock import Mock

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

import drafting_main
from drafting.nodes import approve_outline, outline, route_after_outline_approval
from drafting.state import DocumentType, DraftingState
from shared.schemas.cei_ui import CEIUISurface


def _base_state(**overrides) -> dict:
    state = {
        "messages": [HumanMessage(content="Write a grant proposal about GLP-1")],
        "mode": "science",
        "user_id": "u1",
        "session_id": "s1",
        "document_type": DocumentType.GRANT,
        "guidelines": [],
        "research_questions": [],
        "research_findings": [],
        "outline": "",
        "outline_revision": 0,
        "outline_approved": False,
        "outline_correction": None,
        "trace": [],
    }
    state.update(overrides)
    return state


# ── outline node ─────────────────────────────────────────────────────────────


def test_outline_generates_text_and_bumps_revision(monkeypatch):
    model = Mock()
    model.invoke.return_value = AIMessage(content="- Intro: background\n- Methods: approach")
    monkeypatch.setattr("drafting.nodes.get_model", lambda **_: model)

    result = outline(_base_state())
    assert result["outline"] == "- Intro: background\n- Methods: approach"
    assert result["outline_revision"] == 1
    assert result["outline_correction"] is None


def test_outline_incorporates_correction_and_increments_revision(monkeypatch):
    model = Mock()
    model.invoke.return_value = AIMessage(content="- Intro\n- Methods\n- Appendix")
    monkeypatch.setattr("drafting.nodes.get_model", lambda **_: model)

    state = _base_state(outline="- Intro\n- Methods", outline_revision=1, outline_correction="add an appendix")
    result = outline(state)
    assert result["outline_revision"] == 2
    content_blocks = model.invoke.call_args.args[0][1]["content"]
    # B51a: the outline correction is user-typed, so it must be its own guard_content block.
    assert {"type": "guard_content", "text": "add an appendix"} in content_blocks


# ── approve_outline node: pause + routing on resume ─────────────────────────


def _approve_graph():
    builder = StateGraph(DraftingState)
    builder.add_node("approve_outline", approve_outline)
    builder.add_edge(START, "approve_outline")
    builder.add_edge("approve_outline", END)
    return builder.compile(checkpointer=InMemorySaver())


def test_approve_outline_pauses_with_a_valid_surface():
    graph = _approve_graph()
    config = {"configurable": {"thread_id": "t1"}}
    paused = graph.invoke(_base_state(outline="- Intro\n- Methods", outline_revision=1), config)
    surface = paused["__interrupt__"][0].value
    CEIUISurface.model_validate(surface)
    assert surface["surface_id"] == "drafting-outline"
    types = {c["type"] for c in surface["components"]}
    assert "ReviewGate" in types and "TextField" in types


def test_approve_outline_resume_approve_sets_flag():
    graph = _approve_graph()
    config = {"configurable": {"thread_id": "t2"}}
    graph.invoke(_base_state(outline="- Intro", outline_revision=1), config)
    resumed = graph.invoke(Command(resume={"action_id": "approve-outline", "value": None}), config)
    assert resumed["outline_approved"] is True


def test_approve_outline_resume_reject_cancels():
    graph = _approve_graph()
    config = {"configurable": {"thread_id": "t3"}}
    graph.invoke(_base_state(outline="- Intro", outline_revision=1), config)
    resumed = graph.invoke(Command(resume={"action_id": "reject-outline", "value": None}), config)
    assert resumed["outline_approved"] is False
    assert "cancelled" in str(resumed["messages"][-1].content)


def test_approve_outline_resume_edit_sets_correction():
    graph = _approve_graph()
    config = {"configurable": {"thread_id": "t4"}}
    graph.invoke(_base_state(outline="- Intro", outline_revision=1), config)
    resumed = graph.invoke(
        Command(resume={"action_id": "edit-outline", "value": "add a budget section"}), config
    )
    assert resumed["outline_approved"] is False
    assert resumed["outline_correction"] == "add a budget section"


def test_route_after_outline_approval():
    assert route_after_outline_approval({"outline_approved": True}) == "draft"
    assert route_after_outline_approval({"outline_approved": False, "outline_correction": "x"}) == "outline"
    assert route_after_outline_approval({"outline_approved": False}) == "end"


# ── full graph: outline -> approve_outline -> draft on one checkpointer ────


def test_drafting_graph_pauses_for_outline_then_resumes_into_draft(monkeypatch):
    """A partial graph (outline -> approve_outline -> draft): proves the pause/resume loop
    and that the approved outline reaches `draft`'s state, without exercising the rest of
    the pipeline (research_guidelines/plan_research), which would need more mocked LLM calls."""
    outline_model = Mock()
    outline_model.invoke.return_value = AIMessage(content="- Intro\n- Methods")
    monkeypatch.setattr("drafting.nodes.get_model", lambda **_: outline_model)

    draft_calls = []

    def fake_draft(state):
        draft_calls.append(state.get("outline"))
        return {
            "draft": "Full draft text",
            "draft_document": None,
            "messages": [AIMessage(content="Full draft text")],
            "trace": [],
        }

    builder = StateGraph(DraftingState)
    builder.add_node("outline", outline)
    builder.add_node("approve_outline", approve_outline)
    builder.add_node("draft", fake_draft)
    builder.set_entry_point("outline")
    builder.add_edge("outline", "approve_outline")
    builder.add_conditional_edges(
        "approve_outline", route_after_outline_approval, {"draft": "draft", "outline": "outline", "end": END}
    )
    builder.add_edge("draft", END)
    graph = builder.compile(checkpointer=InMemorySaver())

    config = {"configurable": {"thread_id": "t5"}}
    state = _base_state()
    paused = graph.invoke(state, config)
    assert paused["__interrupt__"][0].value["surface_id"] == "drafting-outline"

    resumed = graph.invoke(Command(resume={"action_id": "approve-outline", "value": None}), config)
    assert resumed["draft"] == "Full draft text"
    assert draft_calls == ["- Intro\n- Methods"]


# ── drafting_main op=resume ──────────────────────────────────────────────────


class _CompletedGraph:
    def __init__(self, result: dict) -> None:
        self._result = result

    async def ainvoke(self, state, config):
        return self._result


def test_drafting_main_resume_without_checkpoint_is_expired(monkeypatch):
    payload = {
        "op": "resume",
        "action": {
            "request_id": "r1",
            "session_id": "0b0c4f7e-5555-4a55-8a55-000000000001",
            "surface_id": "drafting-outline",
            "revision": 1,
            "interaction_id": "drafting-outline-review",
            "component_id": "outline-review",
            "action_id": "approve-outline",
            "value": None,
        },
    }

    class _Context:
        session_id = "0b0c4f7e-5555-4a55-8a55-000000000001"
        request_headers = {"Authorization": "Bearer test-token:test-user"}

    response = asyncio.run(drafting_main.invoke(payload, _Context()))
    assert response["status"] == "rejected" and response["code"] == "expired"


def test_drafting_main_resume_completes_run(monkeypatch):
    from drafting.nodes import _build_outline_approval_surface

    result = {
        "messages": [HumanMessage(content="q"), AIMessage(content="Final draft.")],
        "trace": [],
        "exports": [],
    }
    import drafting.graph as graph_module
    monkeypatch.setattr(graph_module, "compile_graph", lambda **_: _CompletedGraph(result))

    surface = _build_outline_approval_surface("- Intro", revision=1)
    sid = "0b0c4f7e-5555-4a55-8a55-000000000002"
    drafting_main._pending.register(sid, surface)

    class _Context:
        session_id = sid
        request_headers = {"Authorization": "Bearer test-token:test-user"}

    gate = next(c for c in surface["components"] if c["type"] == "ReviewGate")
    payload = {
        "op": "resume",
        "action": {
            "request_id": "r1",
            "session_id": sid,
            "surface_id": "drafting-outline",
            "revision": 1,
            "interaction_id": gate["interaction_id"],
            "component_id": gate["id"],
            "action_id": "approve-outline",
            "value": None,
        },
    }
    response = asyncio.run(drafting_main.invoke(payload, _Context()))
    assert response["status"] == "completed"
    assert response["text"] == "Final draft."
    assert response["action_response"]["status"] == "completed"
