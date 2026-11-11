"""The four inline HITL tools really pause the graph (F13): a one-node graph on
InMemorySaver calls each tool directly as its node body; the tool's own
``interrupt()`` must surface the built CEI UI surface, and ``Command(resume=...)``
must hand the resumed value back as the tool's return value.
"""
from __future__ import annotations

from typing import Any, TypedDict

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from shared.schemas.cei_ui import CEIUISurface
from shared.tools.hitl.inline_functions import (
    AlternativeInput,
    InlineFunctionRegistry,
    PlanStepInput,
)


class _State(TypedDict, total=False):
    result: Any


def _graph_for(tool_call):
    builder = StateGraph(_State)
    builder.add_node("ask", lambda state: {"result": tool_call()})
    builder.add_edge(START, "ask")
    builder.add_edge("ask", END)
    return builder.compile(checkpointer=InMemorySaver())


def _run_and_resume(tool_call, resume_value):
    graph = _graph_for(tool_call)
    config = {"configurable": {"thread_id": "t1"}}
    paused = graph.invoke({}, config)
    interrupts = paused["__interrupt__"]
    assert len(interrupts) == 1
    surface = interrupts[0].value
    CEIUISurface.model_validate(surface)  # the paused value is a complete, valid surface
    resumed = graph.invoke(Command(resume=resume_value), config)
    return surface, resumed["result"]


def test_ask_user_question_pauses_and_resumes():
    registry = InlineFunctionRegistry()
    surface, answer = _run_and_resume(
        lambda: registry._ask_user_question("Pick one", ["A", "B"]),
        {"action_id": "submit-answer", "value": "A"},
    )
    assert surface["surface_id"] == "hitl-ask-user-question"
    assert answer == {"action_id": "submit-answer", "value": "A"}


def test_present_alternatives_pauses_and_resumes():
    registry = InlineFunctionRegistry()
    alternatives = [
        AlternativeInput(title="One", description="First"),
        AlternativeInput(title="Two", description="Second"),
    ]
    surface, answer = _run_and_resume(
        lambda: registry._present_alternatives("Pick", alternatives),
        {"action_id": "select-alternative", "value": "alternative-1"},
    )
    assert surface["surface_id"] == "hitl-present-alternatives"
    assert answer["value"] == "alternative-1"


def test_request_plan_approval_pauses_and_resumes():
    registry = InlineFunctionRegistry()
    steps = [PlanStepInput(title="Search"), PlanStepInput(title="Draft")]
    surface, answer = _run_and_resume(
        lambda: registry._request_plan_approval("Plan", steps),
        {"action_id": "approve-plan", "value": None},
    )
    assert surface["surface_id"] == "hitl-plan-approval"
    assert answer["action_id"] == "approve-plan"


def test_get_user_input_pauses_and_resumes():
    registry = InlineFunctionRegistry()
    surface, answer = _run_and_resume(
        lambda: registry._get_user_input("Describe the target"),
        {"action_id": "submit-input", "value": "GLP-1"},
    )
    assert surface["surface_id"] == "hitl-get-user-input"
    assert answer["value"] == "GLP-1"


@pytest.mark.parametrize("revision", [1, 2, 7])
def test_builders_accept_surface_id_and_revision(revision):
    from shared.tools.hitl.inline_functions import build_ask_user_question_surface

    surface = build_ask_user_question_surface(
        "Pick one", ["A", "B"], surface_id="custom-id", revision=revision
    )
    assert surface["surface_id"] == "custom-id"
    assert surface["revision"] == revision
