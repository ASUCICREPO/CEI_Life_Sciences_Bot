"""Job ID Store adapter with the in-memory ``JobRegistry`` interface (F16).

Deployed runtimes set ``CEI_JOB_TABLE`` and get this durable registry; local
runs keep the in-memory registry (B94). Creating a job takes the user's single
background slot (B22) and finishing it releases the slot. A write that loses
to a user cancellation is reported, not raised, so a cancelled job ends quietly.
"""
from __future__ import annotations

import logging
import os
from collections import OrderedDict
from typing import Any, Callable, Iterable

from shared.persistence.active_runs import ActiveRunRepository, ConcurrencyLimitError
from shared.persistence.jobs import JobNotFoundError, JobRepository, JobTransitionError, public_job
from shared.runtime.jobs import JOB_REGISTRY, JobRecord, JobRegistry, JobStatus, JobStep

log = logging.getLogger(__name__)
_OWNER_CACHE = 256
StatusUiBuilder = Callable[[JobRecord], dict[str, Any]]

__all__ = ["ConcurrencyLimitError", "DurableJobRegistry", "active_job_store", "job_store"]


def _record(item: dict[str, Any]) -> JobRecord:
    return JobRecord(
        job_id=item["job_id"], session_id=item["session_id"], agent_type=item["agent_type"],
        user_id=item.get("user_id"), status=JobStatus(item["status"]),
        steps=[JobStep(step_id=s["step_id"], agent=s["agent"], description=s["description"],
                       status=s.get("status", "idle"), error=s.get("error"), detail=s.get("detail"))
               for s in item.get("steps") or []],
        final_ui=item.get("ui"), final_text=item.get("text"),
        revision=int(item.get("revision", 1)), updated_at=item.get("updated_at", ""),
    )


class DurableJobRegistry:
    durable = True

    def __init__(self, jobs: JobRepository, runs: ActiveRunRepository | None,
                 status_ui_builder: StatusUiBuilder | None = None) -> None:
        self._jobs = jobs
        self._runs = runs
        self.status_ui_builder = status_ui_builder
        # job_id -> owner for jobs this process started; every lookup still goes to the owner's partition.
        self._owners: OrderedDict[str, str] = OrderedDict()

    def _owner(self, job_id: str) -> str:
        owner = self._owners.get(job_id)
        if owner is None:
            raise KeyError(f"unknown job {job_id!r}")
        return owner

    def _remember(self, job_id: str, user_id: str) -> None:
        self._owners[job_id] = user_id
        self._owners.move_to_end(job_id)
        while len(self._owners) > _OWNER_CACHE:
            self._owners.popitem(last=False)

    def create(self, *, session_id: str, agent_type: str, steps: Iterable[tuple[str, str, str]] = (),
               job_id: str | None = None, user_id: str | None = None, runtime_session_id: str | None = None,
               scopes: Iterable[str] = ()) -> JobRecord:
        """Raises ``ConcurrencyLimitError`` when the user already has a background job (B22)."""
        if not user_id:
            raise ValueError("a durable job needs a verified owner")
        from shared.persistence.jobs import new_job_id

        job_id = job_id or new_job_id()
        if self._runs is not None:
            self._runs.acquire_background(user_id, job_id)
        try:
            item = self._jobs.create(user_id=user_id, session_id=session_id, agent_type=agent_type,
                                     runtime_session_id=runtime_session_id or session_id, steps=steps,
                                     job_id=job_id, scopes=scopes)
        except BaseException:
            if self._runs is not None:
                self._runs.release_background(user_id, job_id)
            raise
        self._remember(job_id, user_id)
        return _record(item)

    def get(self, job_id: str) -> JobRecord | None:
        owner = self._owners.get(job_id)
        item = self._jobs.get(owner, job_id) if owner else None
        return _record(item) if item else None

    def get_owned(self, job_id: str, *, user_id: str, session_id: str) -> JobRecord | None:
        item = self._jobs.get_owned(job_id, user_id=user_id, session_id=session_id)
        if item is None:
            return None
        self._remember(job_id, user_id)
        return _record(item)

    def set_status(self, job_id: str, status: JobStatus) -> None:
        owner = self._owner(job_id)
        if JobStatus(status) is JobStatus.RUNNING:
            self._jobs.mark_running(owner, job_id)
        else:
            self._jobs.set_status(owner, job_id, JobStatus(status).value)

    def update_step(self, job_id: str, step_id: str, status: str, *, error: str | None = None,
                    detail: str | None = None) -> None:
        """Raises ``JobTransitionError`` once the job is cancelled: the cooperative stop at a step boundary."""
        self._jobs.update_step(self._owner(job_id), job_id, step_id, status, error=error, detail=detail)

    def heartbeat(self, job_id: str) -> bool:
        """True when the job should stop: the user cancelled it or it is no longer active."""
        try:
            return self._jobs.heartbeat(self._owner(job_id), job_id)
        except (JobTransitionError, JobNotFoundError):
            return True

    def finish(self, job_id: str, status: JobStatus, *, ui: dict[str, Any] | None, text: str | None,
               error: str | None = None) -> bool:
        owner = self._owner(job_id)
        try:
            self._jobs.finish(owner, job_id, JobStatus(status).value, ui=ui, text=text, error=error)
            return True
        except JobTransitionError:
            log.info("job already final; late %s ignored", JobStatus(status).value)
            return False
        finally:
            if self._runs is not None:
                self._runs.release_background(owner, job_id)

    def snapshot(self, job_id: str) -> dict[str, Any]:
        item = self._jobs.get(self._owner(job_id), job_id)
        if item is None:
            raise KeyError(f"unknown job {job_id!r}")
        snapshot = public_job(item)
        if snapshot.get("ui") is None and not snapshot.get("result_in_history") and self.status_ui_builder:
            snapshot["ui"] = self.status_ui_builder(_record(item))
        return snapshot


_active: DurableJobRegistry | JobRegistry | None = None


def job_store(*, status_ui_builder: StatusUiBuilder | None = None) -> DurableJobRegistry | JobRegistry:
    """The process-wide job store: durable when CEI_JOB_TABLE is configured, else the local registry."""
    global _active
    table = os.environ.get("CEI_JOB_TABLE", "").strip()
    if not table:
        JOB_REGISTRY.status_ui_builder = status_ui_builder
        _active = JOB_REGISTRY
        return JOB_REGISTRY
    import boto3

    region = os.environ.get("CEI_AWS_REGION") or None
    runs_table = os.environ.get("CEI_ACTIVE_RUNS_TABLE", "").strip()
    dynamodb = boto3.resource("dynamodb", region_name=region)
    runs = ActiveRunRepository(dynamodb.Table(runs_table)) if runs_table else None
    _active = DurableJobRegistry(JobRepository(table, dynamodb=dynamodb), runs, status_ui_builder)
    return _active


def active_job_store() -> DurableJobRegistry | JobRegistry:
    return _active or JOB_REGISTRY
