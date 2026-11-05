"""In-memory background job registry for the local MVP runtimes (B94, B98).

The snapshot shape is the ``GET /jobs/{jobId}`` shape from ``api.md`` plus the
B98 additions ``steps`` and ``ui``. F16 replaces this registry with the Job ID
Store and the Polling Lambda; keep callers on the public methods only.
"""

from __future__ import annotations

import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Callable, Iterable

MAX_JOBS = 64


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_JOB_STATUSES = frozenset({JobStatus.COMPLETED, JobStatus.PARTIAL, JobStatus.FAILED, JobStatus.CANCELLED})

# Step status values match pipeline_manager.state.StepStatusValue.
_DONE_STEP_STATUSES = frozenset({"completed", "partial", "failed"})


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class JobStep:
    step_id: str
    agent: str
    description: str
    status: str = "idle"
    error: str | None = None
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "agent": self.agent,
            "description": self.description,
            "status": self.status,
            "error": self.error,
            "detail": self.detail,
        }


@dataclass
class JobRecord:
    job_id: str
    session_id: str
    agent_type: str
    # Verified Cognito sub of the user who started the job; job_status answers only that user.
    user_id: str | None = None
    status: JobStatus = JobStatus.PENDING
    steps: list[JobStep] = field(default_factory=list)
    final_ui: dict[str, Any] | None = None
    final_text: str | None = None
    revision: int = 1
    updated_at: str = field(default_factory=_now)

    @property
    def progress_pct(self) -> int:
        if self.status in TERMINAL_JOB_STATUSES:
            return 100
        if not self.steps:
            return 0
        done = sum(1 for step in self.steps if step.status in _DONE_STEP_STATUSES)
        return int(done * 100 / len(self.steps))

    def step(self, step_id: str) -> JobStep:
        for step in self.steps:
            if step.step_id == step_id:
                return step
        raise KeyError(f"unknown step {step_id!r}")


StatusUiBuilder = Callable[[JobRecord], dict[str, Any]]


class JobRegistry:
    """Thread-safe, bounded job store. Every change bumps ``revision``."""

    def __init__(self, limit: int = MAX_JOBS, status_ui_builder: StatusUiBuilder | None = None) -> None:
        self._limit = limit
        self._lock = threading.Lock()
        self._jobs: OrderedDict[str, JobRecord] = OrderedDict()
        self.status_ui_builder = status_ui_builder

    def create(
        self,
        *,
        session_id: str,
        agent_type: str,
        steps: Iterable[tuple[str, str, str]] = (),
        job_id: str | None = None,
        user_id: str | None = None,
    ) -> JobRecord:
        record = JobRecord(
            job_id=job_id or f"job-{uuid.uuid4().hex[:12]}",
            session_id=session_id,
            agent_type=agent_type,
            user_id=user_id,
            steps=[JobStep(step_id=s, agent=a, description=d) for s, a, d in steps],
        )
        with self._lock:
            self._jobs[record.job_id] = record
            while len(self._jobs) > self._limit:
                self._jobs.popitem(last=False)
        return record

    def get(self, job_id: str) -> JobRecord | None:
        with self._lock:
            return self._jobs.get(job_id)

    def get_owned(self, job_id: str, *, user_id: str, session_id: str) -> JobRecord | None:
        """The job only if it was started by `user_id` in `session_id`; a job without an owner is never returned."""
        record = self.get(job_id)
        if record is None or not record.user_id:
            return None
        if record.user_id != user_id or record.session_id != session_id:
            return None
        return record

    def _touch(self, record: JobRecord) -> None:
        record.revision += 1
        record.updated_at = _now()

    def set_status(self, job_id: str, status: JobStatus) -> None:
        with self._lock:
            record = self._jobs[job_id]
            record.status = JobStatus(status)
            self._touch(record)

    def update_step(
        self,
        job_id: str,
        step_id: str,
        status: str,
        *,
        error: str | None = None,
        detail: str | None = None,
    ) -> None:
        with self._lock:
            record = self._jobs[job_id]
            step = record.step(step_id)
            step.status = status
            step.error = error
            if detail is not None:
                step.detail = detail
            self._touch(record)

    def finish(
        self,
        job_id: str,
        status: JobStatus,
        *,
        ui: dict[str, Any] | None,
        text: str | None,
    ) -> None:
        with self._lock:
            record = self._jobs[job_id]
            record.status = JobStatus(status)
            record.final_ui = ui
            record.final_text = text
            self._touch(record)

    def snapshot(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                raise KeyError(f"unknown job {job_id!r}")
            ui = record.final_ui
            if ui is None and self.status_ui_builder is not None:
                ui = self.status_ui_builder(record)
            return {
                "job_id": record.job_id,
                "session_id": record.session_id,
                "agent_type": record.agent_type,
                "status": record.status.value,
                "progress_pct": record.progress_pct,
                "updated_at": record.updated_at,
                "revision": record.revision,
                "steps": [step.as_dict() for step in record.steps],
                "text": record.final_text,
                "ui": ui,
            }


JOB_REGISTRY = JobRegistry()
