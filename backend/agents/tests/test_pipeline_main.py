from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import pipeline_main
from pipeline_manager.state import PipelinePlan, PipelineStep
from shared.tools.hitl.inline_functions import PlanStepInput, build_plan_approval_surface


class _Context:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.request_headers = {"Authorization": "Bearer test-token:test-user"}


def _plan() -> PipelinePlan:
    return PipelinePlan(
        summary="Research then draft",
        steps=[PipelineStep(step_id="step-1", agent="research", description="Find sources")],
        estimated_duration_seconds=30,
    )


def test_failed_job_records_safe_error(monkeypatch):
    graph = SimpleNamespace(ainvoke=AsyncMock(side_effect=RuntimeError("private request content")))
    finish = Mock()
    logger = Mock()
    monkeypatch.setattr(pipeline_main, "_compile_graph", lambda: graph)
    monkeypatch.setattr(pipeline_main.JOB_REGISTRY, "finish", finish)
    monkeypatch.setattr(pipeline_main, "end_turn", AsyncMock())
    monkeypatch.setattr(pipeline_main.app, "complete_async_task", Mock())
    monkeypatch.setattr(pipeline_main, "log", logger)
    asyncio.run(pipeline_main._run_job("job-test", "session-test", "user-test", {}, 1))
    assert finish.call_args.kwargs["text"] == "Pipeline run failed."
    assert "RuntimeError" in str(logger.error.call_args)
    assert "private request content" not in str(logger.error.call_args)
    assert "private request content" not in str(finish.call_args)


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
            surface = build_plan_approval_surface(
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

    session_id = "0b0c4f7e-4444-4a44-8a44-000000000001"

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
    session_id = "0b0c4f7e-4444-4a44-8a44-000000000002"

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
    owner_session = "0b0c4f7e-4444-4a44-8a44-000000000003"
    other_session = "0b0c4f7e-4444-4a44-8a44-000000000004"

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


_JOB_SESSION = "0b0c4f7e-4444-4a44-8a44-000000000010"


def _spy_get_owned(monkeypatch) -> list:
    calls: list = []
    real = pipeline_main.JOB_REGISTRY.get_owned

    def spy(job_id, *, user_id, session_id):
        calls.append({"job_id": job_id, "user_id": user_id, "session_id": session_id})
        return real(job_id, user_id=user_id, session_id=session_id)

    monkeypatch.setattr(pipeline_main.JOB_REGISTRY, "get_owned", spy)
    return calls


@pytest.mark.parametrize(
    ("owner", "job_session"),
    [
        ("someone-else", _JOB_SESSION),  # another user's job in the same session id
        ("test-user", "0b0c4f7e-4444-4a44-8a44-000000000011"),  # caller's job, but another session
        (None, _JOB_SESSION),  # legacy job without an owner is never returned
    ],
    ids=["other-user", "other-session", "no-owner"],
)
def test_job_status_uses_get_owned_and_refuses_with_generic_message(monkeypatch, owner, job_session) -> None:
    record = pipeline_main.JOB_REGISTRY.create(session_id=job_session, agent_type="pipeline", user_id=owner)
    calls = _spy_get_owned(monkeypatch)
    response = asyncio.run(
        pipeline_main.invoke({"op": "job_status", "job_id": record.job_id}, _Context(_JOB_SESSION))
    )
    assert response == {"status": "error", "message": "Job not found."}
    assert calls == [{"job_id": record.job_id, "user_id": "test-user", "session_id": _JOB_SESSION}]


def test_job_status_returns_snapshot_for_owned_job(monkeypatch) -> None:
    record = pipeline_main.JOB_REGISTRY.create(session_id=_JOB_SESSION, agent_type="pipeline", user_id="test-user")
    calls = _spy_get_owned(monkeypatch)
    response = asyncio.run(
        pipeline_main.invoke({"op": "job_status", "job_id": record.job_id}, _Context(_JOB_SESSION))
    )
    assert response.get("message") != "Job not found."
    assert response["job_id"] == record.job_id
    assert len(calls) == 1


class _FakeReplay:
    enabled = True
    mode = SimpleNamespace(value="replay")

    def __init__(self, result=None) -> None:
        self.result = result
        self.job_status_calls: list = []

    def on_job_status(self, job_id, *, session_id):
        self.job_status_calls.append((job_id, session_id))
        return self.result


def test_job_status_replay_not_consulted_when_session_ownership_fails(monkeypatch) -> None:
    import shared.runtime.history as history
    from shared.persistence.chat_metadata import SessionNotFoundError

    class _ForeignRecorder:
        async def assert_owner(self, **_):
            raise SessionNotFoundError(_JOB_SESSION)

    monkeypatch.setattr(history, "get_recorder", lambda: _ForeignRecorder())
    replay = _FakeReplay(result={"status": "completed", "text": "replayed fixture data"})
    monkeypatch.setattr(pipeline_main, "_replay", replay)
    calls = _spy_get_owned(monkeypatch)
    response = asyncio.run(pipeline_main.invoke({"op": "job_status", "job_id": "job-x"}, _Context(_JOB_SESSION)))
    assert response.status_code == 404
    assert b"replayed fixture data" not in response.body
    assert replay.job_status_calls == []
    assert calls == []


def test_job_status_replay_miss_still_enforces_job_ownership(monkeypatch) -> None:
    record = pipeline_main.JOB_REGISTRY.create(session_id=_JOB_SESSION, agent_type="pipeline", user_id="someone-else")
    replay = _FakeReplay(result=None)
    monkeypatch.setattr(pipeline_main, "_replay", replay)
    calls = _spy_get_owned(monkeypatch)
    response = asyncio.run(
        pipeline_main.invoke({"op": "job_status", "job_id": record.job_id}, _Context(_JOB_SESSION))
    )
    assert response == {"status": "error", "message": "Job not found."}
    assert replay.job_status_calls == [(record.job_id, _JOB_SESSION)]
    assert len(calls) == 1
