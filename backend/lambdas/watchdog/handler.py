"""Scheduled watchdog for background jobs (F16, B25).

Running jobs whose heartbeat stopped are failed, or (when resume is enabled
and a checkpoint exists) moved back to pending once so an authenticated caller
can resume them. Pending jobs that never started are failed. Jobs awaiting user
approval are never touched: HITL waits are deliberately long (B57, B82).

Every change is conditional on the job being unchanged since it was read, so a
heartbeat that lands during the scan always wins. Freed jobs release their
background slot (the counter-leak repair in data-model.md section 6).
"""
from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3

from http_errors import get_logger
from shared.persistence.active_runs import ActiveRunRepository
from shared.persistence.jobs import PENDING, RUNNING, JobRepository

logger = get_logger(__name__)
STALE_ERROR = "The background job stopped responding."
NEVER_STARTED_ERROR = "The background job did not start."


def _cutoff(seconds: int, now: float) -> str:
    return (datetime.fromtimestamp(now, UTC) - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def sweep(jobs: JobRepository, runs: ActiveRunRepository, *, now: float, stale_seconds: int,
          pending_seconds: int, resume_enabled: bool, max_recoveries: int, batch: int = 50) -> dict[str, int]:
    counts = {"failed": 0, "requeued": 0, "skipped": 0}
    for item in jobs.stale(RUNNING, updated_before=_cutoff(stale_seconds, now), limit=batch):
        if resume_enabled and jobs.requeue_if_unchanged(item, max_attempts=max_recoveries):
            counts["requeued"] += 1  # keeps its background slot; the resume reuses it
        elif jobs.fail_if_unchanged(item, STALE_ERROR):
            runs.release_background(item["user_id"], item["job_id"])
            counts["failed"] += 1
        else:
            counts["skipped"] += 1
    for item in jobs.stale(PENDING, updated_before=_cutoff(pending_seconds, now), limit=batch):
        if jobs.fail_if_unchanged(item, NEVER_STARTED_ERROR):
            runs.release_background(item["user_id"], item["job_id"])
            counts["failed"] += 1
        else:
            counts["skipped"] += 1
    return counts


def handler(event: dict[str, Any], context: Any) -> dict[str, int]:
    dynamodb = boto3.resource("dynamodb")
    counts = sweep(
        JobRepository(os.environ["CEI_JOB_TABLE"], dynamodb=dynamodb),
        ActiveRunRepository(dynamodb.Table(os.environ["CEI_ACTIVE_RUNS_TABLE"])),
        now=time.time(),
        stale_seconds=int(os.environ.get("CEI_JOB_STALE_SECONDS", "600")),
        pending_seconds=int(os.environ.get("CEI_JOB_PENDING_SECONDS", "3600")),
        resume_enabled=os.environ.get("CEI_WATCHDOG_RESUME_ENABLED", "").lower() == "true",
        max_recoveries=int(os.environ.get("CEI_JOB_MAX_RECOVERIES", "1")),
    )
    # Counts only: job and user identifiers stay out of operational logs.
    logger.info("watchdog_sweep", extra={"status": counts})
    return counts
