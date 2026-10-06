from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import pipeline_main
from pipeline_manager.state import PipelinePlan, PipelineStep
from shared.tools.hitl.inline_functions import InlineFunctionRegistry, PlanStepInput


class _Context:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id


def _plan() -> PipelinePlan:
    return PipelinePlan(
        summary="Research then draft",
        steps=[PipelineStep(step_id="step-1", agent="research", description="Find sources")],
        estimated_duration_seconds=30,
    )


class _FakeStatus:
    def __init__(self, step_id: str, agent: str, status: str, text: str = "") -> None:
        self.step_id = step_id
        self.agent = agent
        self.status = SimpleNamespace(value=status)
        self.result = {"text": text, "document": None}


class _FakeSnapshot:
    def __init__(self, values: dict) -> None:
        self.values = values


class _FakeGraph:
    """Stands in for the compiled pipeline graph: first call interrupts for approval,
    the resumed call (inside the background job) returns a completed run."""

    def __init__(self, plan: PipelinePlan) -> None:
        self._plan = plan
        self.resumed = False

    async def ainvoke(self, arg, config):
        if isinstance(arg, dict):
            surface = InlineFunctionRegistry()._request_plan_approval(
                self._plan.summary,
                [PlanStepInput(title=s.step_id, description=s.description) for s in self._plan.steps],
            )
            return {"__interrupt__": [SimpleNamespace(value=surface)], "plan": self._plan}
        self.resumed = True
        return {
            "steps": [_FakeStatus("step-1", "research", "completed", "Research done")],
            "synthesis": "Final synthesized answer",
            "response_document": None,
            "messages": [],
        }

    async def aget_state(self, config):
        return _FakeSnapshot({"plan": self._plan})


def _gate(surface: dict) -> dict:
    return next(c for c in surface["components"] if c["type"] == "ReviewGate")


def _approve_action(surface: dict, session_id: str, action_id: str = "approve-plan") -> dict:
    gate = _gate(surface)
    return {
        "request_id": "r1",
        "session_id": session_id,
        "surface_id": surface["surface_id"],
        "revision": surface["revision"],
        "interaction_id": gate["interaction_id"],
        "component_id": gate["id"],
        "action_id": action_id,
        "value": None,
    }


@pytest.fixture(autouse=True)
def _reset_runtime_state(monkeypatch):
    monkeypatch.setenv("CEI_DEMO_REPLAY", "off")
    pipeline_main._pending._pending.clear()
    pipeline_main._thread_prompts.clear()
    pipeline_main._thread_ids.clear()
    pipeline_main._background_tasks.clear()
    yield


def test_run_then_resume_then_job_status_completes(monkeypatch) -> None:
    plan = _plan()
    fake_graph = _FakeGraph(plan)
    monkeypatch.setattr(pipeline_main, "_compile_graph", lambda: fake_graph)

    session_id = "sess-main-1"

    async def _scenario():
        run_response = await pipeline_main.invoke(
            {"prompt": "Research TP53 and draft a pitch"}, _Context(session_id)
        )
        assert run_response["status"] == "awaiting_input"

        action = _approve_action(run_response["ui"], session_id)
        resume_response = await pipeline_main.invoke({"op": "resume", "action": action}, _Context(session_id))
        assert resume_response["status"] == "running"
        job_id = resume_response["job_id"]

        task = pipeline_main._background_tasks.get(job_id)
        if task is not None:
            await task

        status_response = await pipeline_main.invoke(
            {"op": "job_status", "job_id": job_id}, _Context(session_id)
        )
        return run_response, resume_response, status_response

    run_response, resume_response, status_response = asyncio.run(_scenario())

    assert fake_graph.resumed is True
    assert status_response["status"] == "completed"
    assert "Final synthesized answer" in status_response["text"]
    assert status_response["ui"]["surface_id"] == "pipeline-final"


def test_reject_plan_does_not_create_a_job(monkeypatch) -> None:
    plan = _plan()
    fake_graph = _FakeGraph(plan)
    monkeypatch.setattr(pipeline_main, "_compile_graph", lambda: fake_graph)
    session_id = "sess-main-2"

    async def _scenario():
        run_response = await pipeline_main.invoke({"prompt": "Do something"}, _Context(session_id))
        action = _approve_action(run_response["ui"], session_id, action_id="reject-plan")
        return await pipeline_main.invoke({"op": "resume", "action": action}, _Context(session_id))

    resume_response = asyncio.run(_scenario())
    assert resume_response["status"] == "completed"
    assert "job_id" not in resume_response
    assert not pipeline_main._background_tasks


def test_job_status_is_isolated_by_session(monkeypatch) -> None:
    plan = _plan()
    fake_graph = _FakeGraph(plan)
    monkeypatch.setattr(pipeline_main, "_compile_graph", lambda: fake_graph)
    owner_session = "sess-owner"
    other_session = "sess-other"

    async def _scenario():
        run_response = await pipeline_main.invoke({"prompt": "Owner task"}, _Context(owner_session))
        action = _approve_action(run_response["ui"], owner_session)
        resume_response = await pipeline_main.invoke({"op": "resume", "action": action}, _Context(owner_session))
        job_id = resume_response["job_id"]
        task = pipeline_main._background_tasks.get(job_id)
        if task is not None:
            await task
        return job_id, await pipeline_main.invoke({"op": "job_status", "job_id": job_id}, _Context(other_session))

    job_id, status_response = asyncio.run(_scenario())
    assert status_response == {"status": "error", "message": "Job not found."}
