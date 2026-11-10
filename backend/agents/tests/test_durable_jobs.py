"""Durable job adapter (F16) and the Pipeline runtime's use of it."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import boto3
import pytest
from moto import mock_aws

from shared.persistence import jobs as job_store
from shared.persistence.active_runs import ActiveRunRepository, ConcurrencyLimitError
from shared.persistence.jobs import JobRepository
from shared.runtime import durable_jobs
from shared.runtime.durable_jobs import DurableJobRegistry
from shared.runtime.jobs import JOB_REGISTRY, JobStatus

STEPS = [("s1", "research", "Find"), ("s2", "drafting", "Draft")]


@pytest.fixture
def registry(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="jobs", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}, {"AttributeName": "job_id", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": n, "AttributeType": "S"}
                                  for n in ("user_id", "job_id", "status", "updated_at")],
            GlobalSecondaryIndexes=[{"IndexName": job_store.STATUS_INDEX, "Projection": {"ProjectionType": "KEYS_ONLY"},
                                     "KeySchema": [{"AttributeName": "status", "KeyType": "HASH"},
                                                   {"AttributeName": "updated_at", "KeyType": "RANGE"}]}])
        ddb.create_table(
            TableName="runs", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}, {"AttributeName": "record", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"},
                                  {"AttributeName": "record", "AttributeType": "S"}])
        runs = ActiveRunRepository(ddb.Table("runs"))
        yield DurableJobRegistry(JobRepository("jobs", dynamodb=ddb), runs,
                                 status_ui_builder=lambda record: {"steps": len(record.steps)}), runs


def test_create_takes_the_single_background_slot(registry):
    jobs, runs = registry
    record = jobs.create(session_id="chat", agent_type="pipeline_manager", steps=STEPS, user_id="alice")
    assert runs.background_holder("alice") == record.job_id
    with pytest.raises(ConcurrencyLimitError):
        jobs.create(session_id="chat-2", agent_type="pipeline_manager", user_id="alice")
    # another user is unaffected
    jobs.create(session_id="chat", agent_type="pipeline_manager", user_id="bob")


def test_failed_create_releases_the_slot(registry):
    jobs, runs = registry
    with pytest.raises(ValueError):
        jobs.create(session_id="chat", agent_type="unknown", user_id="alice")
    assert runs.background_holder("alice") is None


def test_lifecycle_snapshot_matches_local_registry_shape(registry):
    jobs, runs = registry
    record = jobs.create(session_id="chat", agent_type="pipeline_manager", steps=STEPS, user_id="alice")
    jobs.set_status(record.job_id, JobStatus.RUNNING)
    jobs.update_step(record.job_id, "s1", "completed", detail="ok")
    snapshot = jobs.snapshot(record.job_id)
    assert snapshot["status"] == "running" and snapshot["progress_pct"] == 50
    assert snapshot["ui"] == {"steps": 2}  # live status surface while running
    assert {"job_id", "session_id", "status", "progress_pct", "steps", "ui", "text", "revision"} <= set(snapshot)
    assert jobs.finish(record.job_id, JobStatus.COMPLETED, ui={"final": True}, text="done") is True
    assert jobs.snapshot(record.job_id)["ui"] == {"final": True}
    assert runs.background_holder("alice") is None


def test_cancelled_job_stops_steps_and_late_finish_is_quiet(registry):
    jobs, runs = registry
    record = jobs.create(session_id="chat", agent_type="pipeline_manager", steps=STEPS, user_id="alice")
    jobs.set_status(record.job_id, JobStatus.RUNNING)
    jobs._jobs.request_cancel("alice", record.job_id)
    assert jobs.heartbeat(record.job_id) is True
    with pytest.raises(job_store.JobTransitionError):
        jobs._jobs.finish("alice", record.job_id, "completed")
    jobs._jobs.finish("alice", record.job_id, "cancelled")
    with pytest.raises(job_store.JobTransitionError):
        jobs.update_step(record.job_id, "s1", "running")
    assert jobs.finish(record.job_id, JobStatus.COMPLETED, ui=None, text="late") is False
    assert jobs.snapshot(record.job_id)["status"] == "cancelled"


def test_get_owned_is_partitioned_by_user(registry):
    jobs, _ = registry
    record = jobs.create(session_id="chat", agent_type="pipeline_manager", user_id="alice")
    assert jobs.get_owned(record.job_id, user_id="bob", session_id="chat") is None
    assert jobs.get_owned(record.job_id, user_id="alice", session_id="other") is None
    assert jobs.get_owned(record.job_id, user_id="alice", session_id="chat").job_id == record.job_id


def test_job_store_defaults_to_local_registry(monkeypatch):
    monkeypatch.delenv("CEI_JOB_TABLE", raising=False)
    # restored at teardown: the shared registry's builder is what pipeline_main uses
    monkeypatch.setattr(JOB_REGISTRY, "status_ui_builder", JOB_REGISTRY.status_ui_builder)
    monkeypatch.setattr(durable_jobs, "_active", durable_jobs._active)
    builder = Mock()
    assert durable_jobs.job_store(status_ui_builder=builder) is JOB_REGISTRY
    assert JOB_REGISTRY.status_ui_builder is builder
    assert durable_jobs.active_job_store() is JOB_REGISTRY
    assert JOB_REGISTRY.heartbeat("anything") is False


def test_heartbeat_cancels_the_running_job(monkeypatch):
    import pipeline_main

    monkeypatch.setattr(pipeline_main, "HEARTBEAT_SECONDS", 0)
    monkeypatch.setattr(pipeline_main.JOB_REGISTRY, "heartbeat", lambda job_id: True)

    async def scenario():
        job = asyncio.create_task(asyncio.sleep(10))
        await pipeline_main._heartbeat("job", job)
        await asyncio.sleep(0)
        return job.cancelled()

    assert asyncio.run(scenario()) is True


def test_cancelled_run_finishes_as_cancelled(monkeypatch):
    import pipeline_main

    graph = SimpleNamespace(ainvoke=AsyncMock(side_effect=asyncio.CancelledError()))
    finish = Mock()
    monkeypatch.setattr(pipeline_main, "_compile_graph", lambda: graph)
    monkeypatch.setattr(pipeline_main.JOB_REGISTRY, "finish", finish)
    monkeypatch.setattr(pipeline_main, "end_turn", AsyncMock())
    monkeypatch.setattr(pipeline_main.app, "complete_async_task", Mock())
    asyncio.run(pipeline_main._run_job("job", "session", "user", {}, 1))
    assert finish.call_args.args[1] is JobStatus.CANCELLED


def test_concurrency_limit_keeps_the_plan_pending(monkeypatch):
    import pipeline_main
    from pipeline_manager.state import PipelinePlan, PipelineStep
    from shared.tools.hitl.inline_functions import InlineFunctionRegistry, PlanStepInput

    plan = PipelinePlan(summary="Research then draft", estimated_duration_seconds=30,
                        steps=[PipelineStep(step_id="step-1", agent="research", description="Find sources")])

    class Graph:
        async def ainvoke(self, arg, config):
            surface = InlineFunctionRegistry()._request_plan_approval(
                plan.summary, [PlanStepInput(title=s.step_id, description=s.description) for s in plan.steps])
            return {"__interrupt__": [SimpleNamespace(value=surface)], "plan": plan}

        async def aget_state(self, config):
            return SimpleNamespace(values={"plan": plan})

    def context():
        return SimpleNamespace(session_id=session_id, request_headers={"Authorization": "Bearer test-token:test-user"})

    def approve(surface):
        gate = next(c for c in surface["components"] if c["type"] == "ReviewGate")
        return {"request_id": "r1", "session_id": session_id, "surface_id": surface["surface_id"],
                "revision": surface["revision"], "interaction_id": gate["interaction_id"],
                "component_id": gate["id"], "action_id": "approve-plan", "value": None}

    monkeypatch.setenv("CEI_DEMO_REPLAY", "off")
    session_id = "0b0c4f7e-4444-4a44-8a44-000000000009"
    monkeypatch.setattr(pipeline_main, "_compile_graph", lambda: Graph())
    pipeline_main._pending._pending.clear()

    def at_limit(**_):
        raise ConcurrencyLimitError("busy")

    async def scenario():
        run = await pipeline_main.invoke({"prompt": "Research then draft"}, context())
        monkeypatch.setattr(pipeline_main.JOB_REGISTRY, "create", at_limit)
        refused = await pipeline_main.invoke({"op": "resume", "action": approve(run["ui"])}, context())
        return refused, pipeline_main._pending.get(session_id)

    refused, still_pending = asyncio.run(scenario())
    assert refused["code"] == "concurrency_limit"
    assert refused["action_response"]["status"] == "rejected"
    assert still_pending is not None
