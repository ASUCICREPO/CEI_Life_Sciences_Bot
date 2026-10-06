from __future__ import annotations

import asyncio
from typing import TypedDict

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, StateGraph
from langgraph.types import Command, interrupt

from shared.runtime import (
    JobRegistry,
    JobStatus,
    PendingInterrupts,
    RuntimeOp,
    action_response,
    check_resume,
    extract_interrupt_surface,
    parse_action,
    parse_op,
    resume_value,
)
from shared.tools.hitl.inline_functions import InlineFunctionRegistry, PlanStepInput


def _plan_surface() -> dict:
    return InlineFunctionRegistry()._request_plan_approval(
        plan_summary="Two steps",
        steps=[PlanStepInput(title="Research", description="Find sources")],
    )


def _action_for(surface: dict, action_id: str, *, session_id: str = "s1", revision: int | None = None) -> dict:
    gate = next(c for c in surface["components"] if c["type"] == "ReviewGate")
    return {
        "request_id": "r1",
        "session_id": session_id,
        "surface_id": surface["surface_id"],
        "revision": revision if revision is not None else surface["revision"],
        "interaction_id": gate["interaction_id"],
        "component_id": gate["id"],
        "action_id": action_id,
        "value": None,
    }


def test_parse_op_defaults_to_run_and_rejects_unknown() -> None:
    assert parse_op({}) is RuntimeOp.RUN
    assert parse_op({"op": "job_status"}) is RuntimeOp.JOB_STATUS
    with pytest.raises(ValueError):
        parse_op({"op": "delete"})


def test_parse_action_requires_valid_request() -> None:
    with pytest.raises(ValueError):
        parse_action({})
    with pytest.raises(ValueError):
        parse_action({"action": {"request_id": "r1"}})


def test_resume_matches_pending_surface_and_echoes_correlation() -> None:
    surface = _plan_surface()
    pending = PendingInterrupts()
    pending.register("s1", surface)
    action = parse_action({"action": _action_for(surface, "approve-plan")})

    assert check_resume(pending, "s1", action) is None
    response = action_response(action, "completed")
    for key in ("request_id", "session_id", "surface_id", "revision", "interaction_id", "component_id", "action_id", "value"):
        assert response[key] == getattr(action, key)
    assert resume_value(action)["action_id"] == "approve-plan"


def test_resume_is_stale_for_wrong_revision_action_or_no_pending() -> None:
    surface = _plan_surface()
    pending = PendingInterrupts()
    pending.register("s1", surface)

    wrong_revision = parse_action({"action": _action_for(surface, "approve-plan", revision=surface["revision"] + 1)})
    wrong_action = parse_action({"action": _action_for(surface, "not-an-action")})
    assert check_resume(pending, "s1", wrong_revision)["status"] == "stale"
    assert check_resume(pending, "s1", wrong_action)["status"] == "stale"

    pending.clear("s1")
    ok = parse_action({"action": _action_for(surface, "approve-plan")})
    assert check_resume(pending, "s1", ok)["status"] == "stale"


def test_resume_rejects_other_session() -> None:
    surface = _plan_surface()
    pending = PendingInterrupts()
    pending.register("s1", surface)
    action = parse_action({"action": _action_for(surface, "approve-plan", session_id="other")})
    assert check_resume(pending, "s1", action)["status"] == "rejected"


def test_pending_interrupts_are_bounded() -> None:
    surface = _plan_surface()
    pending = PendingInterrupts(limit=2)
    for thread in ("a", "b", "c"):
        pending.register(thread, surface)
    assert pending.get("a") is None
    assert pending.get("c") is not None


class _GateState(TypedDict):
    answer: str


def _gate_node(state: _GateState) -> dict:
    decision = interrupt(_plan_surface())
    return {"answer": decision["action_id"]}


def test_interrupt_round_trip_with_in_memory_saver() -> None:
    builder = StateGraph(_GateState)
    builder.add_node("gate", _gate_node)
    builder.set_entry_point("gate")
    builder.add_edge("gate", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "s1"}}

    first = asyncio.run(graph.ainvoke({"answer": ""}, config=config))
    surface = extract_interrupt_surface(first)
    assert surface is not None and surface["surface_id"] == "hitl-plan-approval"

    action = parse_action({"action": _action_for(surface, "approve-plan")})
    final = asyncio.run(graph.ainvoke(Command(resume=resume_value(action)), config=config))
    assert extract_interrupt_surface(final) is None
    assert final["answer"] == "approve-plan"


def test_job_registry_snapshot_progress_and_revision() -> None:
    registry = JobRegistry(status_ui_builder=lambda record: {"revision": record.revision})
    record = registry.create(
        session_id="s1",
        agent_type="pipeline_manager",
        steps=[("step-1", "research", "Find"), ("step-2", "drafting", "Write")],
    )
    registry.set_status(record.job_id, JobStatus.RUNNING)
    registry.update_step(record.job_id, "step-1", "completed")

    snap = registry.snapshot(record.job_id)
    assert snap["status"] == "running"
    assert snap["progress_pct"] == 50
    assert [s["status"] for s in snap["steps"]] == ["completed", "idle"]
    assert snap["ui"] == {"revision": snap["revision"]}

    registry.finish(record.job_id, JobStatus.COMPLETED, ui={"final": True}, text="done")
    snap = registry.snapshot(record.job_id)
    assert snap["progress_pct"] == 100
    assert snap["ui"] == {"final": True}
    assert snap["text"] == "done"


def test_job_registry_unknown_job_raises() -> None:
    with pytest.raises(KeyError):
        JobRegistry().snapshot("missing")
