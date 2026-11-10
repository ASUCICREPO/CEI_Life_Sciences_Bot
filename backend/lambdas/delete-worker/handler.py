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
