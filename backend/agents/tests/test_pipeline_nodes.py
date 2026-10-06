from __future__ import annotations

import asyncio
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

import pipeline_manager.nodes as nodes
from pipeline_manager.dispatcher import StepOutcome
from pipeline_manager.graph import compile_graph
from pipeline_manager.state import PipelinePlan, PipelineStep, StepStatus, StepStatusValue
from shared.schemas.cei_ui import CEIUISurface


def _structured_model(plan: PipelinePlan | None) -> Mock:
    raw = AIMessage(content="", usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})
    structured = Mock()
    structured.invoke.return_value = {"parsed": plan, "raw": raw}
    model = Mock()
    model.with_structured_output.return_value = structured
    return model


def _state(**overrides) -> dict:
    base = {
        "messages": [HumanMessage(content="Research TP53 and draft a pitch")],
        "mode": "science",
        "user_id": "u1",
        "session_id": "s1",
        "plan": None,
        "plan_approved": False,
        "steps": [],
        "synthesis": "",
        "retry_count": 0,
        "response_document": None,
        "trace": [],
    }
    base.update(overrides)
    return base


# ── plan validation ─────────────────────────────────────────────────────────


def test_plan_rejects_cycle_and_falls_back(monkeypatch) -> None:
    bad_plan = PipelinePlan(
        summary="cyclic",
        steps=[
            PipelineStep(step_id="a", agent="research", description="x", dependencies=["b"]),
            PipelineStep(step_id="b", agent="research", description="y", dependencies=["a"]),
        ],
        estimated_duration_seconds=10,
    )
    model = _structured_model(bad_plan)
    monkeypatch.setattr(nodes, "get_model", lambda *a, **k: model)

    result = nodes.plan(_state())

    assert len(result["plan"].steps) == 1
    assert result["plan"].steps[0].agent == "research"
    assert any(entry.stage == "PLAN_FALLBACK" for entry in result["trace"])
    assert model.with_structured_output.return_value.invoke.call_count == 2  # one repair retry


def test_plan_rejects_unknown_dependency(monkeypatch) -> None:
    bad_plan = PipelinePlan(
        summary="bad dep",
        steps=[PipelineStep(step_id="a", agent="research", description="x", dependencies=["missing"])],
        estimated_duration_seconds=10,
    )
    model = _structured_model(bad_plan)
    monkeypatch.setattr(nodes, "get_model", lambda *a, **k: model)

    result = nodes.plan(_state())
    assert len(result["plan"].steps) == 1
    assert result["plan"].steps[0].step_id == "step-1"


def test_plan_rejects_too_many_steps(monkeypatch) -> None:
    steps = [PipelineStep(step_id=f"s{i}", agent="research", description="x") for i in range(7)]
    bad_plan = PipelinePlan(summary="too many", steps=steps, estimated_duration_seconds=10)
    model = _structured_model(bad_plan)
    monkeypatch.setattr(nodes, "get_model", lambda *a, **k: model)

    result = nodes.plan(_state())
    assert len(result["plan"].steps) == 1


def test_plan_accepts_valid_plan_on_first_try(monkeypatch) -> None:
    good_plan = PipelinePlan(
        summary="ok",
        steps=[
            PipelineStep(step_id="a", agent="research", description="x"),
            PipelineStep(step_id="b", agent="drafting", description="y", dependencies=["a"]),
        ],
        estimated_duration_seconds=10,
    )
    model = _structured_model(good_plan)
    monkeypatch.setattr(nodes, "get_model", lambda *a, **k: model)

    result = nodes.plan(_state())
    assert result["plan"] is good_plan
    assert model.with_structured_output.return_value.invoke.call_count == 1
    assert [s.step_id for s in result["steps"]] == ["a", "b"]


# ── should_continue regression ──────────────────────────────────────────────


def test_should_continue_routes_on_status() -> None:
    assert nodes.should_continue(_state(steps=[])) == "dispatch"
    running = [StepStatus(step_id="a", agent="research", status=StepStatusValue.RUNNING)]
    assert nodes.should_continue(_state(steps=running)) == "monitor"
    failed = [StepStatus(step_id="a", agent="research", status=StepStatusValue.FAILED)]
    assert nodes.should_continue(_state(steps=failed, retry_count=0)) == "dispatch"
    assert nodes.should_continue(_state(steps=failed, retry_count=3)) == "synthesize"
    done = [StepStatus(step_id="a", agent="research", status=StepStatusValue.COMPLETED)]
    assert nodes.should_continue(_state(steps=done)) == "synthesize"


# ── dispatch: ready-wave logic ──────────────────────────────────────────────


def _plan_two_sequential() -> PipelinePlan:
    return PipelinePlan(
        summary="seq",
        steps=[
            PipelineStep(step_id="a", agent="research", description="first"),
            PipelineStep(step_id="b", agent="drafting", description="second", dependencies=["a"]),
        ],
        estimated_duration_seconds=10,
    )


def _plan_two_concurrent() -> PipelinePlan:
    return PipelinePlan(
        summary="concurrent",
        steps=[
            PipelineStep(step_id="a", agent="research", description="first"),
            PipelineStep(step_id="b", agent="drafting", description="second"),
        ],
        estimated_duration_seconds=10,
    )


class _FakeDispatcher:
    def __init__(self, outcomes: dict[str, StepOutcome], events: list | None = None) -> None:
        self._outcomes = outcomes
        self._events = events if events is not None else []

    async def run(self, step, *, session_id, mode, user_id, dependency_outputs):
        self._events.append(("start", step.step_id))
        await asyncio.sleep(0.01)
        self._events.append(("end", step.step_id))
        return self._outcomes[step.step_id]


def test_dispatch_runs_sequential_waves_in_order_with_dependency_output(monkeypatch) -> None:
    outcomes = {"a": StepOutcome(status="completed", text="A done"), "b": StepOutcome(status="completed", text="B done")}
    events: list = []
    seen_deps: dict = {}
    fake = _FakeDispatcher(outcomes, events)
    original_run = fake.run

    async def _recording_run(step, **kwargs):
        seen_deps[step.step_id] = dict(kwargs["dependency_outputs"])
        return await original_run(step, **kwargs)

    fake.run = _recording_run
    monkeypatch.setattr(nodes, "_dispatcher", fake)

    plan_obj = _plan_two_sequential()
    steps = [StepStatus(step_id="a", agent="research"), StepStatus(step_id="b", agent="drafting")]
    state = _state(plan=plan_obj, steps=steps)

    result = asyncio.run(nodes.dispatch(state, {"configurable": {}}))
    statuses = {s.step_id: s.status for s in result["steps"]}
    # One dispatch runs every ready wave: should_continue goes to synthesize when nothing is RUNNING/FAILED.
    assert statuses == {"a": StepStatusValue.COMPLETED, "b": StepStatusValue.COMPLETED}
    assert events == [("start", "a"), ("end", "a"), ("start", "b"), ("end", "b")]
    assert seen_deps["a"] == {}
    assert "A done" in seen_deps["b"]["a"]


def test_dispatch_stops_after_failed_wave_for_retry(monkeypatch) -> None:
    outcomes = {"a": StepOutcome(status="failed", error="boom"), "b": StepOutcome(status="completed", text="B")}
    fake = _FakeDispatcher(outcomes)
    monkeypatch.setattr(nodes, "_dispatcher", fake)

    plan_obj = _plan_two_sequential()
    steps = [StepStatus(step_id="a", agent="research"), StepStatus(step_id="b", agent="drafting")]
    result = asyncio.run(nodes.dispatch(_state(plan=plan_obj, steps=steps), {"configurable": {}}))
    statuses = {s.step_id: s.status for s in result["steps"]}
    assert statuses == {"a": StepStatusValue.FAILED, "b": StepStatusValue.IDLE}
    assert nodes.should_continue({**_state(plan=plan_obj, steps=result["steps"]), "retry_count": result["retry_count"]}) == "dispatch"


def test_dispatch_runs_concurrent_steps_in_parallel(monkeypatch) -> None:
    outcomes = {"a": StepOutcome(status="completed", text="A"), "b": StepOutcome(status="completed", text="B")}
    events: list = []
    fake = _FakeDispatcher(outcomes, events)
    monkeypatch.setattr(nodes, "_dispatcher", fake)

    plan_obj = _plan_two_concurrent()
    steps = [StepStatus(step_id="a", agent="research"), StepStatus(step_id="b", agent="drafting")]
    state = _state(plan=plan_obj, steps=steps)

    result = asyncio.run(nodes.dispatch(state, {"configurable": {}}))
    statuses = {s.step_id: s.status for s in result["steps"]}
    assert statuses == {"a": StepStatusValue.COMPLETED, "b": StepStatusValue.COMPLETED}
    # both steps started before either ended -> ran concurrently
    starts = [e for e in events if e[0] == "start"]
    ends = [e for e in events if e[0] == "end"]
    assert len(starts) == 2
    assert events.index(ends[0]) > events.index(starts[1])


def test_dispatch_second_wave_runs_dependent_step(monkeypatch) -> None:
    outcomes = {"b": StepOutcome(status="completed", text="B done")}
    fake = _FakeDispatcher(outcomes)
    monkeypatch.setattr(nodes, "_dispatcher", fake)

    plan_obj = _plan_two_sequential()
    steps = [
        StepStatus(step_id="a", agent="research", status=StepStatusValue.COMPLETED, result={"text": "A done"}),
        StepStatus(step_id="b", agent="drafting"),
    ]
    state = _state(plan=plan_obj, steps=steps)

    result = asyncio.run(nodes.dispatch(state, {"configurable": {}}))
    statuses = {s.step_id: s.status for s in result["steps"]}
    assert statuses["b"] == StepStatusValue.COMPLETED


def test_dispatch_retries_failure_then_marks_partial(monkeypatch) -> None:
    plan_obj = PipelinePlan(
        summary="flaky",
        steps=[PipelineStep(step_id="a", agent="research", description="x")],
        estimated_duration_seconds=10,
    )

    calls = {"n": 0}

    class _FlakyDispatcher:
        async def run(self, step, *, session_id, mode, user_id, dependency_outputs):
            calls["n"] += 1
            return StepOutcome(status="failed", error=f"attempt {calls['n']} failed")

    monkeypatch.setattr(nodes, "_dispatcher", _FlakyDispatcher())

    steps = [StepStatus(step_id="a", agent="research")]
    state = _state(plan=plan_obj, steps=steps, retry_count=0)

    config = {"configurable": {}}
    # Wave 1: initial attempt -> FAILED, retry_count stays 0 (not a retry wave)
    result = asyncio.run(nodes.dispatch(state, config))
    assert result["steps"][0].status == StepStatusValue.FAILED
    assert result["retry_count"] == 0

    state = _state(plan=plan_obj, steps=result["steps"], retry_count=result["retry_count"])
    # Waves 2 and 3: retries, still failing
    for _ in range(2):
        result = asyncio.run(nodes.dispatch(state, config))
        assert result["steps"][0].status == StepStatusValue.FAILED
        state = _state(plan=plan_obj, steps=result["steps"], retry_count=result["retry_count"])

    assert state["retry_count"] == 2
    # Wave 4: retry_count becomes 3 (== MAX_RETRIES) -> exhausted -> partial
    result = asyncio.run(nodes.dispatch(state, config))
    assert result["steps"][0].status == StepStatusValue.PARTIAL
    assert result["retry_count"] == 3
    assert calls["n"] == 4


# ── approve / reject through a real graph ───────────────────────────────────


def _good_plan() -> PipelinePlan:
    return PipelinePlan(
        summary="Two steps",
        steps=[PipelineStep(step_id="step-1", agent="research", description="Find sources")],
        estimated_duration_seconds=30,
    )


def test_approve_and_dispatch_through_real_graph(monkeypatch) -> None:
    plan_model = _structured_model(_good_plan())
    monkeypatch.setattr(nodes, "get_model", lambda *a, **k: plan_model)
    monkeypatch.setattr(nodes, "_dispatcher", _FakeDispatcher({"step-1": StepOutcome(status="completed", text="done")}))

    def _fake_invoke_structured(model, schema, messages):
        from shared.schemas.response_blocks import ProseBlock, ResponseDocument, ResponseSection

        doc = ResponseDocument(
            title="Pipeline result",
            summary="done",
            sections=[ResponseSection(heading="Answer", blocks=[ProseBlock(kind="prose", markdown="done")])],
        )
        return doc, AIMessage(content=""), None

    monkeypatch.setattr(nodes, "invoke_structured", _fake_invoke_structured)

    graph = compile_graph(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "t1"}}
    first = asyncio.run(graph.ainvoke(_state(), config=config))
    assert "__interrupt__" in first

    surface_dict = first["__interrupt__"][0].value
    surface = CEIUISurface.model_validate(surface_dict)
    gate = next(c for c in surface.components if c.type == "ReviewGate")

    final = asyncio.run(graph.ainvoke(
        Command(resume={"action_id": "approve-plan", "interaction_id": gate.interaction_id, "component_id": gate.id, "value": None}),
        config=config,
    ))
    assert final["plan_approved"] is True
    assert final["steps"][0].status == StepStatusValue.COMPLETED
    assert "done" in final["synthesis"]


def test_reject_plan_ends_run_without_dispatch(monkeypatch) -> None:
    plan_model = _structured_model(_good_plan())
    monkeypatch.setattr(nodes, "get_model", lambda *a, **k: plan_model)
    dispatcher_calls: list = []

    class _NeverCalled:
        async def run(self, *a, **k):
            dispatcher_calls.append(1)
            raise AssertionError("dispatch must not run after rejection")

    monkeypatch.setattr(nodes, "_dispatcher", _NeverCalled())

    graph = compile_graph(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "t2"}}
    first = asyncio.run(graph.ainvoke(_state(), config=config))
    surface = CEIUISurface.model_validate(first["__interrupt__"][0].value)
    gate = next(c for c in surface.components if c.type == "ReviewGate")

    final = asyncio.run(graph.ainvoke(
        Command(resume={"action_id": "reject-plan", "interaction_id": gate.interaction_id, "component_id": gate.id, "value": None}),
        config=config,
    ))
    assert final["plan_approved"] is False
    assert not dispatcher_calls
