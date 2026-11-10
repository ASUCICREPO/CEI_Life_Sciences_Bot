"""Durable Job ID Store (data-model.md section 3, B25, B98).

Items are keyed by ``user_id`` (Cognito sub) and ``job_id``. Job IDs embed a
millisecond timestamp, so a descending query lists a user's newest jobs first.
The ``status-updated_at`` index lets the watchdog find active jobs whose
heartbeat stopped without scanning the table.

Every state change is a conditional write. Terminal jobs never change again, so
cancellation, completion and watchdog recovery cannot all succeed for one job.
This module must not import LangGraph: it ships in the REST Lambda layer.
"""
from __future__ import annotations

import json
import re
import secrets
import time
from datetime import UTC, datetime
from typing import Any, Iterable

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from shared.persistence.page import Page, from_dynamo

STATUS_INDEX = "status-updated_at"
PENDING, RUNNING, AWAITING_APPROVAL = "pending", "running", "awaiting_approval"
COMPLETED, PARTIAL, FAILED, CANCELLED = "completed", "partial", "failed", "cancelled"
ACTIVE_STATUSES = frozenset({PENDING, RUNNING, AWAITING_APPROVAL})
TERMINAL_STATUSES = frozenset({COMPLETED, PARTIAL, FAILED, CANCELLED})
AGENT_TYPES = frozenset({"research", "drafting", "analysis", "pipeline_manager"})
STEP_STATUSES = frozenset({"idle", "running", "completed", "partial", "failed", "skipped"})
_DONE_STEPS = frozenset({"completed", "partial", "failed", "skipped"})
_JOB_ID = re.compile(r"^job_[0-9]{13}_[0-9a-f]{16}$")
_STEP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
MAX_STEPS = 32
MAX_TEXT_CHARS = 2000
MAX_ERROR_CHARS = 300
# DynamoDB items are capped at 400 KB; a larger final surface is served from chat history instead.
MAX_UI_BYTES = 200_000
PUBLIC_FIELDS = ("job_id", "session_id", "agent_type", "status", "progress_pct", "steps", "ui", "text",
                 "error", "cancel_requested", "result_in_history", "revision", "created_at", "updated_at")


class JobNotFoundError(Exception):
    """No job with this id exists for this user."""


class JobTransitionError(Exception):
    """The job is not in a state that allows the requested change."""


def valid_job_id(value: Any) -> bool:
    return isinstance(value, str) and _JOB_ID.fullmatch(value) is not None


def new_job_id(now_ms: int | None = None) -> str:
    stamp = int(time.time() * 1000) if now_ms is None else now_ms
    return f"job_{stamp:013d}_{secrets.token_hex(8)}"


def _iso_now() -> str:
    # Fixed width UTC with a Z suffix, so lexicographic order equals time order in the index.
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _redacted(error: str | None) -> str | None:
    if error is None:
        return None
    if not isinstance(error, str):
        raise ValueError("invalid job error")
    return error[:MAX_ERROR_CHARS]


def progress_pct(status: str, steps: list[dict[str, Any]]) -> int:
    if status in TERMINAL_STATUSES:
        return 100
    if not steps:
        return 0
    return int(sum(1 for step in steps if step.get("status") in _DONE_STEPS) * 100 / len(steps))


def public_job(item: dict[str, Any]) -> dict[str, Any]:
    """The polling shape from api.md plus B98 ``steps`` and ``ui``; internal fields stay private."""
    record = from_dynamo(item)
    public = {field: record.get(field) for field in PUBLIC_FIELDS}
    # Derived on read: concurrent step updates write only their own step, never a shared counter.
    public["progress_pct"] = progress_pct(record.get("status", PENDING), record.get("steps") or [])
    return public


class JobRepository:
    def __init__(self, table_name: str, *, dynamodb: Any = None) -> None:
        if not table_name:
            raise ValueError("the job table must be configured")
        self._table = (dynamodb or boto3.resource("dynamodb")).Table(table_name)

    # -- reads ---------------------------------------------------------------

    def get(self, user_id: str, job_id: str) -> dict[str, Any] | None:
        if not user_id or not valid_job_id(job_id):
            return None
        item = self._table.get_item(Key={"user_id": user_id, "job_id": job_id}, ConsistentRead=True).get("Item")
        return from_dynamo(item) if item else None

    def get_owned(self, job_id: str, *, user_id: str, session_id: str) -> dict[str, Any] | None:
        item = self.get(user_id, job_id)
        if item is None or item.get("session_id") != session_id:
            return None
        return item

    def list_for_user(self, user_id: str, *, limit: int, start_key: dict[str, Any] | None = None) -> Page:
        if not user_id:
            raise ValueError("user is required")
        kwargs: dict[str, Any] = {"KeyConditionExpression": Key("user_id").eq(user_id), "Limit": limit,
                                  "ScanIndexForward": False, "ConsistentRead": True}
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        response = self._table.query(**kwargs)
        return Page(items=[from_dynamo(item) for item in response.get("Items", [])],
                    next_key=response.get("LastEvaluatedKey"))

    def stale(self, status: str, *, updated_before: str, limit: int = 50) -> list[dict[str, Any]]:
        """Active jobs whose last write is older than ``updated_before`` (index reads are eventually consistent)."""
        if status not in ACTIVE_STATUSES:
            raise ValueError("only active jobs can be stale")
        response = self._table.query(IndexName=STATUS_INDEX, Limit=limit,
                                     KeyConditionExpression=Key("status").eq(status) & Key("updated_at").lt(updated_before))
        return [from_dynamo(item) for item in response.get("Items", [])]

    # -- writes --------------------------------------------------------------

    def create(self, *, user_id: str, session_id: str, agent_type: str, runtime_session_id: str,
               steps: Iterable[tuple[str, str, str]] = (), job_id: str | None = None,
               checkpoint_ref: str | None = None, scopes: Iterable[str] = ()) -> dict[str, Any]:
        """Create a pending job. ``scopes`` are the run's tool scopes; token refresh reads them from here only."""
        if not user_id or not session_id or not runtime_session_id or agent_type not in AGENT_TYPES:
            raise ValueError("owner, session, runtime session and a known agent type are required")
        scope_list = list(scopes)
        if len(set(scope_list)) != len(scope_list) or any(not isinstance(s, str) or not s for s in scope_list):
            raise ValueError("invalid job scopes")
        job_id = job_id or new_job_id()
        if not valid_job_id(job_id):
            raise ValueError("invalid job id")
        step_items = [self._step(step_id, agent, description) for step_id, agent, description in steps]
        if len(step_items) > MAX_STEPS or len({s["step_id"] for s in step_items}) != len(step_items):
            raise ValueError("invalid job steps")
        now = _iso_now()
        item = {"user_id": user_id, "job_id": job_id, "session_id": session_id, "agent_type": agent_type,
                "status": PENDING, "progress_pct": 0, "steps": step_items, "ui": None, "text": None,
                "error": None, "cancel_requested": False, "result_in_history": False, "revision": 1,
                "runtime_session_id": runtime_session_id, "checkpoint_ref": checkpoint_ref, "scopes": scope_list,
                "recovery_attempts": 0, "heartbeat_at": int(time.time()), "created_at": now, "updated_at": now}
        try:
            self._table.put_item(Item=item, ConditionExpression="attribute_not_exists(job_id)")
        except ClientError as exc:
            if _conditional(exc):
                raise JobTransitionError("job already exists") from None
            raise
        return item

    def mark_running(self, user_id: str, job_id: str, *, runtime_session_id: str | None = None) -> dict[str, Any]:
        values: dict[str, Any] = {"status": RUNNING, "heartbeat_at": int(time.time())}
        if runtime_session_id:
            values["runtime_session_id"] = runtime_session_id
        return self._transition(user_id, job_id, values, allowed=(PENDING, AWAITING_APPROVAL, RUNNING),
                                require_not_cancelled=True)

    def set_status(self, user_id: str, job_id: str, status: str) -> dict[str, Any]:
        if status not in ACTIVE_STATUSES:
            raise ValueError("use finish() for terminal states")
        return self._transition(user_id, job_id, {"status": status}, allowed=tuple(ACTIVE_STATUSES))

    def heartbeat(self, user_id: str, job_id: str, *, checkpoint_ref: str | None = None) -> bool:
        """Record liveness and return whether cancellation was requested."""
        values: dict[str, Any] = {"heartbeat_at": int(time.time())}
        if checkpoint_ref:
            values["checkpoint_ref"] = checkpoint_ref
        item = self._transition(user_id, job_id, values, allowed=(RUNNING, AWAITING_APPROVAL), bump=False)
        return bool(item.get("cancel_requested"))

    def update_step(self, user_id: str, job_id: str, step_id: str, status: str, *,
                    error: str | None = None, detail: str | None = None) -> dict[str, Any]:
        """Update one step in place; concurrent steps never overwrite each other's fields."""
        if status not in STEP_STATUSES:
            raise ValueError("invalid step status")
        item = self.get(user_id, job_id)
        if item is None:
            raise JobNotFoundError(job_id)
        # Step order is fixed at creation, so the index read here stays valid; the condition re-checks it.
        index = next((i for i, step in enumerate(item.get("steps") or []) if step.get("step_id") == step_id), None)
        if index is None:
            raise KeyError(f"unknown step {step_id!r}")
        # DynamoDB rejects unused placeholders, so #sdet is added only when a detail is written.
        names = {"#steps": "steps", "#sid": "step_id", "#sst": "status", "#serr": "error"}
        sets = [(f"#steps[{index}].#sst", ":sst", status), (f"#steps[{index}].#serr", ":serr", _redacted(error))]
        if detail is not None:
            names["#sdet"] = "detail"
            sets.append((f"#steps[{index}].#sdet", ":sdet", detail[:MAX_TEXT_CHARS]))
        return self._transition(user_id, job_id, {}, allowed=(RUNNING,), nested_sets=(sets, names),
                                extra_condition=(f"#steps[{index}].#sid = :sid", names, {":sid": step_id}))

    def finish(self, user_id: str, job_id: str, status: str, *, ui: dict[str, Any] | None = None,
               text: str | None = None, error: str | None = None) -> dict[str, Any]:
        if status not in TERMINAL_STATUSES:
            raise ValueError("finish() requires a terminal status")
        values: dict[str, Any] = {"status": status, "progress_pct": 100, "error": _redacted(error),
                                  "text": text[:MAX_TEXT_CHARS] if isinstance(text, str) else None}
        if ui is not None and len(json.dumps(ui, separators=(",", ":")).encode()) > MAX_UI_BYTES:
            values.update(ui=None, result_in_history=True)
        else:
            values["ui"] = ui
        # A cancelled job may only end as cancelled; a late completion from the runtime loses.
        return self._transition(user_id, job_id, values, allowed=tuple(ACTIVE_STATUSES),
                                require_not_cancelled=status != CANCELLED)

    def request_cancel(self, user_id: str, job_id: str) -> dict[str, Any]:
        return self._transition(user_id, job_id, {"cancel_requested": True}, allowed=tuple(ACTIVE_STATUSES))

    def fail_if_unchanged(self, item: dict[str, Any], error: str) -> bool:
        """Watchdog: fail a stale job only if no write happened since it was observed."""
        try:
            self._transition(item["user_id"], item["job_id"], {"status": FAILED, "error": _redacted(error)},
                             allowed=(item["status"],),
                             extra_condition=("#updated_at = :observed", {}, {":observed": item["updated_at"]}))
            return True
        except (JobTransitionError, JobNotFoundError):
            return False

    def requeue_if_unchanged(self, item: dict[str, Any], *, max_attempts: int) -> bool:
        """Watchdog: move a stale running job back to pending for one checkpoint resume."""
        try:
            self._transition(item["user_id"], item["job_id"], {"status": PENDING}, allowed=(RUNNING,),
                             require_not_cancelled=True, add_recovery=True,
                             extra_condition=("#updated_at = :observed AND #recovery < :max_attempts "
                                              "AND attribute_type(#checkpoint, :string)",
                                              {"#checkpoint": "checkpoint_ref"},
                                              {":observed": item["updated_at"], ":max_attempts": max_attempts,
                                               ":string": "S"}))
            return True
        except (JobTransitionError, JobNotFoundError):
            return False

    # -- internals -----------------------------------------------------------

    @staticmethod
    def _step(step_id: str, agent: str, description: str) -> dict[str, Any]:
        if not isinstance(step_id, str) or _STEP_ID.fullmatch(step_id) is None:
            raise ValueError("invalid step id")
        if not isinstance(agent, str) or not agent or not isinstance(description, str):
            raise ValueError("invalid step")
        return {"step_id": step_id, "agent": agent[:64], "description": description[:MAX_TEXT_CHARS],
                "status": "idle", "error": None, "detail": None}

    def _transition(self, user_id: str, job_id: str, values: dict[str, Any], *, allowed: tuple[str, ...],
                    require_not_cancelled: bool = False, bump: bool = True, add_recovery: bool = False,
                    nested_sets: tuple[list[tuple[str, str, Any]], dict[str, str]] | None = None,
                    extra_condition: tuple[str, dict[str, str], dict[str, Any]] | None = None) -> dict[str, Any]:
        if not user_id or not valid_job_id(job_id):
            raise JobNotFoundError(job_id)
        names: dict[str, str] = {"#status": "status", "#updated_at": "updated_at", "#job_id": "job_id"}
        expr_values: dict[str, Any] = {":now": _iso_now()}
        assignments = ["#updated_at = :now"]
        for i, (field, value) in enumerate(values.items()):
            names[f"#f{i}"] = field
            expr_values[f":v{i}"] = value
            assignments.append(f"#f{i} = :v{i}")
        if nested_sets:
            paths, path_names = nested_sets
            names.update(path_names)
            for path, placeholder, value in paths:
                expr_values[placeholder] = value
                assignments.append(f"{path} = {placeholder}")
        adds = []
        if bump:
            names["#revision"] = "revision"
            expr_values[":one"] = 1
            adds.append("#revision :one")
        if add_recovery:
            names["#recovery"] = "recovery_attempts"
            expr_values[":one"] = 1
            adds.append("#recovery :one")
        allowed_keys = []
        for i, status in enumerate(allowed):
            expr_values[f":s{i}"] = status
            allowed_keys.append(f":s{i}")
        condition = f"attribute_exists(#job_id) AND #status IN ({', '.join(allowed_keys)})"
        if require_not_cancelled:
            names["#cancel"] = "cancel_requested"
            expr_values[":true"] = True
            condition += " AND #cancel <> :true"
        if extra_condition:
            clause, clause_names, clause_values = extra_condition
            condition += f" AND ({clause})"
            names.update(clause_names)
            expr_values.update(clause_values)
            if "#recovery" in clause:
                names["#recovery"] = "recovery_attempts"
        update = "SET " + ", ".join(assignments) + (" ADD " + ", ".join(adds) if adds else "")
        try:
            response = self._table.update_item(Key={"user_id": user_id, "job_id": job_id}, UpdateExpression=update,
                                               ConditionExpression=condition, ExpressionAttributeNames=names,
                                               ExpressionAttributeValues=expr_values, ReturnValues="ALL_NEW")
        except ClientError as exc:
            if _conditional(exc):
                raise JobTransitionError(job_id) from None
            raise
        return from_dynamo(response["Attributes"])


def _conditional(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"
