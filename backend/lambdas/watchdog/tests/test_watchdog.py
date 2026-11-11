import sys
import time
from pathlib import Path
from unittest.mock import Mock

LAMBDAS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(LAMBDAS / "watchdog"))
sys.path.insert(0, str(LAMBDAS / "shared"))
sys.path.insert(0, str(LAMBDAS.parent / "agents"))

import handler as watchdog  # noqa: E402


def job(status="running", **extra):
    return {"user_id": "alice", "job_id": "job_0000000000001_00000000000000aa", "status": status,
            "updated_at": "2026-01-01T00:00:00.000000Z", **extra}


def repos(running=(), pending=()):
    jobs = Mock()
    jobs.stale.side_effect = lambda status, **_: list(running if status == "running" else pending)
    return jobs, Mock()


def run(jobs, runs, *, resume=False):
    return watchdog.sweep(jobs, runs, now=time.time(), stale_seconds=600, pending_seconds=3600,
                          resume_enabled=resume, max_recoveries=1)


def test_stale_running_job_is_failed_and_slot_released():
    jobs, runs = repos(running=[job()])
    jobs.fail_if_unchanged.return_value = True
    assert run(jobs, runs) == {"failed": 1, "requeued": 0, "skipped": 0}
    jobs.requeue_if_unchanged.assert_not_called()
    runs.release_background.assert_called_once_with("alice", job()["job_id"])


def test_heartbeat_during_scan_wins():
    jobs, runs = repos(running=[job()])
    jobs.fail_if_unchanged.return_value = False
    assert run(jobs, runs)["skipped"] == 1
    runs.release_background.assert_not_called()


def test_resume_requeues_and_keeps_slot():
    jobs, runs = repos(running=[job()])
    jobs.requeue_if_unchanged.return_value = True
    assert run(jobs, runs, resume=True)["requeued"] == 1
    jobs.fail_if_unchanged.assert_not_called()
    runs.release_background.assert_not_called()


def test_resume_falls_back_to_failure_without_checkpoint_or_budget():
    jobs, runs = repos(running=[job()])
    jobs.requeue_if_unchanged.return_value = False
    jobs.fail_if_unchanged.return_value = True
    assert run(jobs, runs, resume=True)["failed"] == 1


def test_never_started_pending_job_is_failed():
    jobs, runs = repos(pending=[job("pending")])
    jobs.fail_if_unchanged.return_value = True
    assert run(jobs, runs)["failed"] == 1
    assert jobs.fail_if_unchanged.call_args.args[1] == watchdog.NEVER_STARTED_ERROR


def test_awaiting_approval_jobs_are_never_queried():
    jobs, runs = repos()
    run(jobs, runs)
    assert {call.args[0] for call in jobs.stale.call_args_list} == {"running", "pending"}


def test_cutoffs_match_index_timestamp_format():
    assert watchdog._cutoff(60, 120.0) == "1970-01-01T00:01:00.000000Z"
