"""Fail-closed deletion cascade orchestration; adapters and deployment are pending.

SQS messages reference trusted jobs by ID only. Identity, scope, ownership and
deleting status come from the durable deletion job store, never from the message.
This file is not wired to production until the dependencies in README.md exist.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable, Mapping, Protocol

_JOB_ID = re.compile(r"^[a-f0-9]{32}$")


class DeletionBackend(Protocol):
    def get_job(self, job_id: str) -> Mapping[str, Any] | None: ...
    def verify_deletion_fence(self, job: Mapping[str, Any]) -> None: ...
    def quiesce(self, user_id: str, session_id: str | None) -> None: ...
    def sessions(self, user_id: str) -> Iterable[str]: ...
    def delete_exports(self, user_id: str, session_id: str) -> None: ...
    def delete_history_and_replay(self, user_id: str, session_id: str) -> None: ...
    def delete_checkpoints(self, user_id: str, session_id: str) -> None: ...
    def delete_session_metadata(self, user_id: str, session_id: str) -> None: ...
    def delete_long_term_memory(self, user_id: str) -> None: ...
    def delete_api_keys(self, user_id: str) -> None: ...
    def delete_user_metadata_and_runs(self, user_id: str) -> None: ...
    def delete_cognito_user(self, user_id: str) -> None: ...
    def complete_job(self, job: Mapping[str, Any]) -> None: ...


def process(job_id: str, backend: DeletionBackend) -> None:
    """Repeatable cascade. A failed or unavailable dependency leaves the job pending.

    Adapters must make each deletion idempotent, enumerate all pages, and raise
    on incomplete results. The deletion fence must prevent new writes and exports;
    quiesce must verify runs are stopped before destructive operations begin.
    """
    if not isinstance(job_id, str) or _JOB_ID.fullmatch(job_id) is None:
        raise ValueError("invalid job identifier")
    job = backend.get_job(job_id)
    if not isinstance(job, Mapping) or job.get("job_id") != job_id:
        raise ValueError("unknown deletion job")
    if job.get("status") == "complete":
        return
    if job.get("status") != "deleting":
        raise ValueError("deletion job not active")
    kind, user_id, session_id = job.get("kind"), job.get("user_id"), job.get("session_id")
    if kind not in ("chat", "account") or not isinstance(user_id, str) or not user_id:
        raise ValueError("invalid deletion job")
    if kind == "chat" and (not isinstance(session_id, str) or not session_id):
        raise ValueError("missing chat")
    if kind == "account" and session_id is not None:
        raise ValueError("invalid account deletion job")
    backend.verify_deletion_fence(job)
    backend.quiesce(user_id, session_id)

    def delete_chat(chat_id: str) -> None:
        if not isinstance(chat_id, str) or not chat_id:
            raise ValueError("invalid session")
        # Keep session metadata until all other operations succeed, so account
        # retries can still enumerate partially deleted sessions.
        backend.delete_exports(user_id, chat_id)
        backend.delete_history_and_replay(user_id, chat_id)
        backend.delete_checkpoints(user_id, chat_id)
        backend.delete_session_metadata(user_id, chat_id)

    if kind == "chat":
        delete_chat(session_id)
    else:
        for chat_id in backend.sessions(user_id):
            delete_chat(chat_id)
        backend.delete_long_term_memory(user_id)
        backend.delete_api_keys(user_id)
        backend.delete_user_metadata_and_runs(user_id)
        backend.delete_cognito_user(user_id)
    backend.complete_job(job)


def process_sqs(event: Mapping[str, Any], backend: DeletionBackend) -> dict[str, Any]:
    """Use SQS partial batch failures so successful jobs are not retried together."""
    failures = []
    for record in event.get("Records", []):
        try:
            body = json.loads(record["body"])
            if not isinstance(body, dict) or set(body) != {"job_id"}:
                raise ValueError("invalid deletion message")
            process(body["job_id"], backend)
        except Exception:
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}


# ---------------------------------------------------------------------------------------------------------
# B109: the deployed account-delete cascade below. EventBridge Scheduler invokes `handle` directly with
# `{"user_id": <sub>}`, 9 hours after `POST /account/delete` (`backend/lambdas/account-delete/handler.py`,
# data-model.md "Deletion cascade") -- no SQS, no job/outbox store, no writer fence. The 9-hour delay is
# itself the late-write fix (B109): by the time this runs, any timed-out SDK retry from an in-flight run
# has already finished, so no epoch-conditional write is needed. `process`/`process_sqs`/`DeletionBackend`
# above predate B104/B109 (chat delete + a fenced job store); they are kept, with their tests unchanged,
# because this cascade needs none of that machinery -- see README.md.
# ---------------------------------------------------------------------------------------------------------

import logging
import os
from datetime import UTC, datetime, timedelta

logger = logging.getLogger("delete_worker")
if not logger.handlers:
    logger.addHandler(logging.StreamHandler())
logger.setLevel(logging.INFO)

# ADR: a fixed source-id list, not secretsmanager:ListSecrets | Rationale: B78 names every user key
# `cei/user-keys/{user_id}/{source_id}`; this is the same fixed source set tool-gateway.ts wires
# (B86/B97), so a loop of DeleteSecret calls needs no broader list permission
_DEFAULT_SOURCE_IDS = ("pubmed", "clinicaltrials", "biorxiv", "medrxiv", "chembl", "opentargets",
                       "cmscoverage", "icd10", "npi", "pubchem", "ncbi", "openfda", "patentsview")

RESCHEDULE_DELAY_SECONDS = 120  # short re-check delay (an early sweep, or this invocation ran out of time)
SAFETY_MARGIN_MS = 90_000  # re-schedule and exit before the Lambda's own timeout, not after it


def _region() -> str:
    return os.environ.get("CEI_AWS_REGION", "us-east-1")


def _source_ids() -> list[str]:
    raw = os.environ.get("CEI_API_KEY_SOURCE_IDS", "")
    ids = [item.strip() for item in raw.split(",") if item.strip()]
    return ids or list(_DEFAULT_SOURCE_IDS)


def _gate_repository():
    from shared.persistence.account_gate import AccountGateRepository

    return AccountGateRepository()


def _chat_metadata_repository():
    from shared.persistence.chat_metadata import ChatMetadataRepository

    return ChatMetadataRepository()


def _chat_history_repository():
    from shared.persistence.chat_history import ChatHistoryRepository

    return ChatHistoryRepository()


def _export_repository():
    import boto3

    from shared.persistence.exports import ExportRepository

    return ExportRepository(
        os.environ["CEI_EXPORT_METADATA_TABLE"], os.environ["CEI_EXPORT_BUCKET"],
        dynamodb=boto3.resource("dynamodb", region_name=_region()),
        s3=boto3.client("s3", region_name=_region()),
    )


def _job_repository():
    import boto3

    from shared.persistence.jobs import JobRepository

    return JobRepository(os.environ["CEI_JOB_TABLE"], dynamodb=boto3.resource("dynamodb", region_name=_region()))


def _active_runs_table():
    import boto3

    return boto3.resource("dynamodb", region_name=_region()).Table(os.environ["CEI_ACTIVE_RUNS_TABLE"])


def _secrets_client():
    import boto3

    return boto3.client("secretsmanager", region_name=_region())


def _cognito_client():
    import boto3

    return boto3.client("cognito-idp", region_name=_region())


def _scheduler_client():
    import boto3

    return boto3.client("scheduler", region_name=_region())


def _reschedule(user_id: str, run_at: datetime) -> None:
    """Create (or, if a same-named schedule is still mid-deletion, update) the one-time schedule that
    re-invokes this worker for `user_id`. Idempotent: the schedule name is a deterministic hash of the
    user id (`shared.persistence.account_gate.schedule_name`)."""
    from shared.persistence.account_gate import schedule_name

    client = _scheduler_client()
    kwargs: dict[str, Any] = dict(
        Name=schedule_name(user_id),
        GroupName=os.environ["CEI_SCHEDULE_GROUP_NAME"],
        ScheduleExpression=f"at({run_at.strftime('%Y-%m-%dT%H:%M:%S')})",
        ScheduleExpressionTimezone="UTC",
        FlexibleTimeWindow={"Mode": "OFF"},
        ActionAfterCompletion="DELETE",
        Target={
            "Arn": os.environ["CEI_DELETE_WORKER_ARN"],
            "RoleArn": os.environ["CEI_SCHEDULER_ROLE_ARN"],
            "Input": json.dumps({"user_id": user_id}),
        },
    )
    try:
        client.create_schedule(**kwargs)
    except client.exceptions.ConflictException:
        client.update_schedule(**kwargs)


def _delete_user_secrets(user_id: str) -> None:
    client = _secrets_client()
    prefix = os.environ.get("CEI_API_KEY_SECRET_PREFIX", "cei/user-keys")
    for source_id in _source_ids():
        try:
            client.delete_secret(SecretId=f"{prefix}/{user_id}/{source_id}", ForceDeleteWithoutRecovery=True)
        except client.exceptions.ResourceNotFoundException:
            pass


def _delete_active_run_leftovers(user_id: str) -> None:
    """Remove any Active-run records still left for this user (realtime/background counters, a stray
    route_choice pending pick). Replay index records are removed per-session by `_delete_session` first,
    so only non-replay rows can remain here."""
    from boto3.dynamodb.conditions import Key

    table = _active_runs_table()
    cursor = None
    while True:
        kwargs: dict[str, Any] = {"KeyConditionExpression": Key("user_id").eq(user_id), "ConsistentRead": True}
        if cursor:
            kwargs["ExclusiveStartKey"] = cursor
        page = table.query(**kwargs)
        for item in page.get("Items", []):
            table.delete_item(Key={"user_id": user_id, "record": item["record"]})
        cursor = page.get("LastEvaluatedKey")
        if not cursor:
            return


def _delete_session(user_id: str, session_id: str, memory_id: str | None) -> None:
    from shared.persistence.replay_deletion import delete_session_replays

    history = _chat_history_repository()
    # Exports (S3 objects, then their metadata) before the history they reference.
    _export_repository().delete_session(user_id, session_id)
    # Replay partitions and their ActiveRunsStore index, before the ordinary session partition (handoff
    # notes, F19 phase file): each replay entry names a separate `sse#...` history partition.
    delete_session_replays(user_id=user_id, session_id=session_id, runs_table=_active_runs_table(), history=history)
    # The ordinary partition: turns, trace (incl. chunked trace items) and the legacy DynamoDB-backed
    # checkpoint range (shared/persistence/checkpoints.py) all live under this one session_id partition key.
    history.delete_session(session_id)
    if memory_id:
        from shared.memory.agentcore import delete_checkpoint

        delete_checkpoint(memory_id, session_id, user_id)
    # Session metadata is removed last within this per-session step: a retried cascade can still find and
    # re-process a partially deleted session by listing it from the Session Metadata Store.
    _chat_metadata_repository().delete(user_id, session_id)


def _run_cascade(user_id: str, context: Any) -> str:
    memory_id = os.environ.get("CEI_MEMORY_ID") or None
    chat_metadata = _chat_metadata_repository()

    cursor = None
    while True:
        page = chat_metadata.list_by_user(user_id, limit=25, start_key=cursor)
        for record in page.items:
            if context is not None and context.get_remaining_time_in_millis() < SAFETY_MARGIN_MS:
                _reschedule(user_id, datetime.now(UTC) + timedelta(seconds=RESCHEDULE_DELAY_SECONDS))
                return "rescheduled"
            _delete_session(user_id, record.session_id, memory_id)
        cursor = page.next_key
        if not cursor:
            break

    jobs = _job_repository()
    job_cursor = None
    while True:
        page = jobs.list_for_user(user_id, limit=50, start_key=job_cursor)
        for job in page.items:
            jobs.delete(job["user_id"], job["job_id"])
        job_cursor = page.next_key
        if not job_cursor:
            break

    _delete_active_run_leftovers(user_id)

    if memory_id:
        from shared.memory.agentcore import delete_long_term_namespace

        delete_long_term_namespace(memory_id, user_id)

    _delete_user_secrets(user_id)

    gate_repo = _gate_repository()
    gate = gate_repo.get(user_id)
    cognito_username = gate.cognito_username if gate is not None else None

    from shared.persistence.user_metadata import UserMetadataRepository

    # User Metadata items: the "profile" settings row, then the "deletion" gate row last (B109).
    UserMetadataRepository().delete(user_id)
    if gate is not None:
        gate_repo.delete_gate(user_id)

    if cognito_username:
        cognito = _cognito_client()
        try:
            cognito.admin_delete_user(UserPoolId=os.environ["USER_POOL_ID"], Username=cognito_username)
        except cognito.exceptions.UserNotFoundException:
            pass

    return "complete"


def handle(event: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
    """The scheduled sweep target (B109). `event` is exactly `{"user_id": <sub>}` (never a tool argument
    or anything else caller-supplied -- the account-delete Lambda controls this payload entirely).
    """
    user_id = event.get("user_id") if isinstance(event, Mapping) else None
    if not isinstance(user_id, str) or not user_id:
        raise ValueError("invalid account-delete event")
    gate_repo = _gate_repository()
    gate = gate_repo.get(user_id)
    if gate is None:
        logger.info(json.dumps({"event": "delete_worker", "outcome": "no_gate"}))
        return {"status": "skipped"}
    now = datetime.now(UTC)
    if now < gate.sweep_at:
        # Should not happen in normal operation (the schedule itself fires at sweep_at); this guard means
        # an early or manually-triggered invocation never runs the cascade ahead of B109's 9-hour delay.
        _reschedule(user_id, gate.sweep_at)
        logger.info(json.dumps({"event": "delete_worker", "outcome": "early"}))
        return {"status": "rescheduled"}
    status = _run_cascade(user_id, context)
    logger.info(json.dumps({"event": "delete_worker", "outcome": status}))
    return {"status": status}
