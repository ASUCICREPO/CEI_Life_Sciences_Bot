from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from shared.persistence import jobs
from shared.persistence.jobs import JobRepository, JobTransitionError, new_job_id, public_job, valid_job_id

STEPS = [("s1", "research", "Find papers"), ("s2", "drafting", "Draft memo")]


@pytest.fixture
def repo(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="jobs", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"},
                       {"AttributeName": "job_id", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": n, "AttributeType": "S"}
                                  for n in ("user_id", "job_id", "status", "updated_at")],
            GlobalSecondaryIndexes=[{"IndexName": jobs.STATUS_INDEX, "Projection": {"ProjectionType": "ALL"},
                                     "KeySchema": [{"AttributeName": "status", "KeyType": "HASH"},
                                                   {"AttributeName": "updated_at", "KeyType": "RANGE"}]}])
        yield JobRepository("jobs", dynamodb=ddb)


def create(repo, user="alice", **kwargs):
    return repo.create(user_id=user, session_id="chat-1", agent_type="pipeline_manager",
                       runtime_session_id="rt-1", steps=STEPS, **kwargs)


def test_job_ids_sort_by_time_and_validate():
    assert valid_job_id(new_job_id(1))
    assert new_job_id(1) < new_job_id(2)
    assert not valid_job_id("job-abc") and not valid_job_id(None)


def test_owner_partition_isolates_jobs(repo):
    job = create(repo)
    assert repo.get("bob", job["job_id"]) is None
    assert repo.get_owned(job["job_id"], user_id="alice", session_id="other") is None
    assert repo.get_owned(job["job_id"], user_id="alice", session_id="chat-1")["status"] == "pending"
    with pytest.raises(JobTransitionError):
        repo.request_cancel("bob", job["job_id"])


def test_list_is_newest_first_and_paginates(repo):
    first = create(repo, job_id=new_job_id(1000))
    second = create(repo, job_id=new_job_id(2000))
    page = repo.list_for_user("alice", limit=1)
    assert [i["job_id"] for i in page.items] == [second["job_id"]] and page.next_key
    rest = repo.list_for_user("alice", limit=1, start_key=page.next_key)
    assert [i["job_id"] for i in rest.items] == [first["job_id"]]


def test_concurrent_step_updates_do_not_overwrite(repo):
    job = create(repo)
    repo.mark_running("alice", job["job_id"])
    repo.update_step("alice", job["job_id"], "s1", "running")
    repo.update_step("alice", job["job_id"], "s2", "completed", detail="done")
    steps = repo.get("alice", job["job_id"])["steps"]
    assert [s["status"] for s in steps] == ["running", "completed"]
    assert public_job(repo.get("alice", job["job_id"]))["progress_pct"] == 50
    with pytest.raises(KeyError):
        repo.update_step("alice", job["job_id"], "missing", "running")


def test_terminal_job_never_changes(repo):
    job = create(repo)
    repo.mark_running("alice", job["job_id"])
    repo.finish("alice", job["job_id"], "completed", text="ok")
    for change in (lambda: repo.finish("alice", job["job_id"], "failed"),
                   lambda: repo.request_cancel("alice", job["job_id"]),
                   lambda: repo.update_step("alice", job["job_id"], "s1", "failed"),
                   lambda: repo.heartbeat("alice", job["job_id"])):
        with pytest.raises(JobTransitionError):
            change()
    assert repo.get("alice", job["job_id"])["status"] == "completed"


def test_cancel_beats_late_completion(repo):
    job = create(repo)
    repo.mark_running("alice", job["job_id"])
    repo.request_cancel("alice", job["job_id"])
    assert repo.heartbeat("alice", job["job_id"]) is True
    with pytest.raises(JobTransitionError):
        repo.finish("alice", job["job_id"], "completed")
    repo.finish("alice", job["job_id"], "cancelled")
    assert repo.get("alice", job["job_id"])["status"] == "cancelled"


def test_cancelled_pending_job_cannot_start(repo):
    job = create(repo)
    repo.request_cancel("alice", job["job_id"])
    with pytest.raises(JobTransitionError):
        repo.mark_running("alice", job["job_id"])


def test_large_final_surface_is_left_to_history(repo):
    job = create(repo)
    repo.mark_running("alice", job["job_id"])
    item = repo.finish("alice", job["job_id"], "completed", ui={"blob": "x" * (jobs.MAX_UI_BYTES + 1)})
    assert item["ui"] is None and item["result_in_history"] is True


def test_errors_are_bounded(repo):
    job = create(repo)
    item = repo.finish("alice", job["job_id"], "failed", error="e" * 5000)
    assert len(item["error"]) == jobs.MAX_ERROR_CHARS


def test_watchdog_fail_loses_to_a_newer_heartbeat(repo):
    job = create(repo)
    observed = repo.mark_running("alice", job["job_id"])
    repo.heartbeat("alice", job["job_id"])
    assert repo.fail_if_unchanged(observed, "stopped responding") is False
    fresh = repo.get("alice", job["job_id"])
    assert repo.fail_if_unchanged(fresh, "stopped responding") is True
    assert repo.get("alice", job["job_id"])["status"] == "failed"


def test_requeue_needs_checkpoint_and_respects_attempt_budget(repo):
    job = create(repo)
    observed = repo.mark_running("alice", job["job_id"])
    assert repo.requeue_if_unchanged(observed, max_attempts=1) is False  # no checkpoint yet
    repo.heartbeat("alice", job["job_id"], checkpoint_ref="thread-1")
    observed = repo.get("alice", job["job_id"])
    assert repo.requeue_if_unchanged(observed, max_attempts=1) is True
    again = repo.mark_running("alice", job["job_id"])
    assert again["recovery_attempts"] == 1
    assert repo.requeue_if_unchanged(again, max_attempts=1) is False


def test_stale_query_uses_status_index(repo):
    job = create(repo)
    repo.mark_running("alice", job["job_id"])
    assert [i["job_id"] for i in repo.stale("running", updated_before="9999")] == [job["job_id"]]
    assert repo.stale("running", updated_before="0000") == []
    with pytest.raises(ValueError):
        repo.stale("completed", updated_before="9999")


def test_public_shape_hides_internal_fields(repo):
    job = create(repo)
    public = public_job(job)
    assert "runtime_session_id" not in public and "checkpoint_ref" not in public and "user_id" not in public
    assert public["steps"][0]["step_id"] == "s1"


def test_create_validates_inputs(repo):
    with pytest.raises(ValueError):
        repo.create(user_id="alice", session_id="chat", agent_type="unknown", runtime_session_id="rt")
    with pytest.raises(ValueError):
        repo.create(user_id="alice", session_id="chat", agent_type="research", runtime_session_id="rt",
                    steps=[("s1", "a", "d"), ("s1", "b", "e")])
    job = create(repo)
    with pytest.raises(JobTransitionError):
        create(repo, job_id=job["job_id"])


def test_scopes_are_stored_privately_and_validated(repo):
    job = create(repo, scopes=("cei-tools/src.pubmed",))
    assert repo.get("alice", job["job_id"])["scopes"] == ["cei-tools/src.pubmed"]
    assert "scopes" not in public_job(job)
    with pytest.raises(ValueError):
        create(repo, scopes=("a", "a"))


def test_delete_removes_only_the_one_job(repo):
    job = create(repo)
    other = create(repo, job_id=new_job_id(2))
    repo.delete("alice", job["job_id"])
    assert repo.get("alice", job["job_id"]) is None
    assert repo.get("alice", other["job_id"]) is not None


def test_delete_is_idempotent_and_ignores_an_invalid_id(repo):
    repo.delete("alice", "job_not_real")  # invalid shape: silently ignored
    repo.delete("alice", new_job_id(99))  # never existed: no error
