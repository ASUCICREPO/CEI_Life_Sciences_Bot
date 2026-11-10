"""Atomic account/chat deletion fences and durable in-flight writer tracking.

This repository is a foundation, not a complete deletion implementation. All
writers must acquire a token before their first write and hold it until their last
side effect has completed. Deletion must first install the appropriate fence and
then wait for ``quiescent``. Tokens deliberately have no TTL: expiry alone is
not proof that an isolated runtime or a network request has stopped. Recovery
must confirm the writer is terminated before releasing an abandoned token.

The fence table has string partition key ``user_id`` and string sort key
``scope``. Account and chat gates are durable tombstones until account cleanup
has safely revoked credentials. Session ownership is checked separately by the
caller and the metadata existence check is repeated in the acquire transaction.
"""
from __future__ import annotations

import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Iterator
from uuid import uuid4

from botocore.exceptions import ClientError

from shared.persistence.ownership import is_session_uuid

_TOKEN = re.compile(r"^[a-f0-9]{32}$")


class WriteFenced(RuntimeError):
    """A write was rejected because the chat/account is deleting or missing."""


@dataclass(frozen=True)
class WriterToken:
    user_id: str
    session_id: str
    token: str

    @property
    def scope(self) -> str:
        return f"writer#{self.session_id}#{self.token}"


class WriterFence:
    def __init__(self, client: Any, fence_table: str, metadata_table: str) -> None:
        if not fence_table or not metadata_table or fence_table == metadata_table:
            raise ValueError("distinct fence and session metadata tables are required")
        self._client = client
        self._table = fence_table
        self._metadata_table = metadata_table

    @staticmethod
    def _validate(user_id: str, session_id: str | None = None, job_id: str | None = None) -> None:
        if not isinstance(user_id, str) or not user_id or len(user_id) > 256:
            raise ValueError("invalid user")
        if session_id is not None and (not is_session_uuid(session_id) or session_id != session_id.lower()):
            raise ValueError("invalid session")
        if job_id is not None and (not isinstance(job_id, str) or not _TOKEN.fullmatch(job_id)):
            raise ValueError("invalid deletion job")

    def _key(self, user_id: str, scope: str) -> dict[str, dict[str, str]]:
        return {"user_id": {"S": user_id}, "scope": {"S": scope}}

    def _active_gate(self, user_id: str, scope: str) -> dict[str, Any]:
        return {"ConditionCheck": {
            "TableName": self._table, "Key": self._key(user_id, scope),
            "ConditionExpression": "attribute_not_exists(#state)",
            "ExpressionAttributeNames": {"#state": "state"},
        }}

    @staticmethod
    def _transaction_rejected(exc: ClientError) -> bool:
        if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
            return False
        reasons = exc.response.get("CancellationReasons") or []
        return any(reason.get("Code") == "ConditionalCheckFailed" for reason in reasons)

    def acquire(self, user_id: str, session_id: str, *, writer_ref: str) -> WriterToken:
        self._validate(user_id, session_id)
        if not isinstance(writer_ref, str) or not 1 <= len(writer_ref) <= 256 or any(ord(c) < 32 for c in writer_ref):
            raise ValueError("invalid writer reference")
        token = WriterToken(user_id, session_id, uuid4().hex)
        items = [self._active_gate(user_id, "account"), self._active_gate(user_id, f"chat#{session_id}"),
                 {"ConditionCheck": {
                     "TableName": self._metadata_table,
                     "Key": {"user_id": {"S": user_id}, "session_id": {"S": session_id}},
                     "ConditionExpression": "attribute_exists(session_id)",
                 }},
                 {"Put": {"TableName": self._table,
                          "Item": {**self._key(user_id, token.scope), "token": {"S": token.token},
                                   "writer_ref": {"S": writer_ref}, "created_at": {"S": datetime.now(UTC).isoformat()}},
                          "ConditionExpression": "attribute_not_exists(#scope)",
                          "ExpressionAttributeNames": {"#scope": "scope"}}}]
        try:
            self._client.transact_write_items(TransactItems=items)
        except ClientError as exc:
            if self._transaction_rejected(exc):
                raise WriteFenced("The chat cannot accept writes.") from None
            raise
        return token

    def release(self, token: WriterToken) -> None:
        if not isinstance(token, WriterToken):
            raise ValueError("invalid writer token")
        self._validate(token.user_id, token.session_id, token.token)
        self._client.delete_item(
            TableName=self._table, Key=self._key(token.user_id, token.scope),
            ConditionExpression="attribute_not_exists(#scope) OR #token = :token",
            ExpressionAttributeNames={"#scope": "scope", "#token": "token"},
            ExpressionAttributeValues={":token": {"S": token.token}},
        )

    @contextmanager
    def writing(self, user_id: str, session_id: str, *, writer_ref: str) -> Iterator[WriterToken]:
        token = self.acquire(user_id, session_id, writer_ref=writer_ref)
        try:
            yield token
        except BaseException:
            # A timeout does not establish whether a remote write completed.
            # Recovery must verify termination before releasing this token.
            raise
        else:
            self.release(token)

    def _gate_is(self, user_id: str, scope: str, job_id: str, states: tuple[str, ...]) -> bool:
        gate = self._client.get_item(TableName=self._table, Key=self._key(user_id, scope), ConsistentRead=True).get("Item")
        return bool(gate and gate.get("job_id", {}).get("S") == job_id and gate.get("state", {}).get("S") in states)

    def begin_deletion(self, user_id: str, job_id: str, session_id: str | None = None) -> None:
        self._validate(user_id, session_id, job_id)
        scope = f"chat#{session_id}" if session_id is not None else "account"
        if session_id is None:
            # Fence the account immediately, even when an earlier chat
            # deletion is still running. Quiescence waits for those cascades.
            items = [{"Update": {
                "TableName": self._table, "Key": self._key(user_id, "account"),
                "UpdateExpression": "SET #state = :deleting, job_id = :job",
                "ConditionExpression": "attribute_not_exists(#state) OR (#state = :deleting AND job_id = :job)",
                "ExpressionAttributeNames": {"#state": "state"},
                "ExpressionAttributeValues": {":deleting": {"S": "deleting"}, ":job": {"S": job_id}},
            }}]
        else:
            # The account counter and new chat fence are one transaction. This
            # serializes account deletion with starting/completing chat jobs.
            items = [{"Update": {
                "TableName": self._table, "Key": self._key(user_id, "account"),
                "UpdateExpression": "ADD #active :one",
                "ConditionExpression": "attribute_not_exists(#state)",
                "ExpressionAttributeNames": {"#state": "state", "#active": "active_chat_deletions"},
                "ExpressionAttributeValues": {":one": {"N": "1"}},
            }}, {"Put": {
                "TableName": self._table,
                "Item": {**self._key(user_id, scope), "state": {"S": "deleting"}, "job_id": {"S": job_id}},
                "ConditionExpression": "attribute_not_exists(#scope)",
                "ExpressionAttributeNames": {"#scope": "scope"},
            }}]
        try:
            self._client.transact_write_items(TransactItems=items)
        except ClientError as exc:
            if self._transaction_rejected(exc):
                # A retry must not increment the counter a second time. A
                # completed gate is a tombstone, not permission to restart
                # a cascade after an account deletion has begun.
                if session_id is not None and self._gate_is(user_id, scope, job_id, ("deleting",)):
                    return
                raise WriteFenced("A different deletion is already in progress.") from None
            raise

    def finish_chat_deletion(self, user_id: str, session_id: str, job_id: str) -> None:
        """Release the account deletion barrier only after the entire chat cascade.

        Production integration must commit this transition with durable job
        completion, or use an outbox/recovery protocol which cannot lose either.
        It must never call this merely because writers became quiescent.
        """
        self._validate(user_id, session_id, job_id)
        scope = f"chat#{session_id}"
        try:
            quiet = self.quiescent(user_id, job_id, session_id)
        except WriteFenced:
            if self._gate_is(user_id, scope, job_id, ("deleted",)):
                return
            raise
        if not quiet:
            raise WriteFenced("The chat still has active writers.")
        items = [{"Update": {
            "TableName": self._table, "Key": self._key(user_id, scope),
            "UpdateExpression": "SET #state = :deleted",
            "ConditionExpression": "#state = :deleting AND job_id = :job",
            "ExpressionAttributeNames": {"#state": "state"},
            "ExpressionAttributeValues": {":deleting": {"S": "deleting"}, ":deleted": {"S": "deleted"}, ":job": {"S": job_id}},
        }}, {"Update": {
            "TableName": self._table, "Key": self._key(user_id, "account"),
            "UpdateExpression": "ADD #active :minus_one",
            "ConditionExpression": "(attribute_not_exists(#state) OR #state = :deleting) AND #active >= :one",
            "ExpressionAttributeNames": {"#state": "state", "#active": "active_chat_deletions"},
            "ExpressionAttributeValues": {":minus_one": {"N": "-1"}, ":one": {"N": "1"}, ":deleting": {"S": "deleting"}},
        }}]
        try:
            self._client.transact_write_items(TransactItems=items)
        except ClientError as exc:
            if self._transaction_rejected(exc):
                if self._gate_is(user_id, scope, job_id, ("deleted",)):
                    return
                raise WriteFenced("The chat deletion cannot be completed.") from None
            raise

    def quiescent(self, user_id: str, job_id: str, session_id: str | None = None) -> bool:
        self._validate(user_id, session_id, job_id)
        scope = f"chat#{session_id}" if session_id is not None else "account"
        gate = self._client.get_item(TableName=self._table, Key=self._key(user_id, scope), ConsistentRead=True).get("Item")
        if not gate or gate.get("state", {}).get("S") != "deleting" or gate.get("job_id", {}).get("S") != job_id:
            raise WriteFenced("The deletion fence is missing or does not match this job.")
        if session_id is None:
            try:
                active_chat_deletions = int(gate.get("active_chat_deletions", {"N": "0"})["N"])
            except (KeyError, TypeError, ValueError):
                raise WriteFenced("The account deletion state is invalid.") from None
            if active_chat_deletions < 0:
                raise WriteFenced("The account deletion state is invalid.")
            if active_chat_deletions:
                return False
        prefix = f"writer#{session_id}#" if session_id is not None else "writer#"
        response = self._client.query(
            TableName=self._table, ConsistentRead=True, Limit=1,
            KeyConditionExpression="user_id = :user AND begins_with(#scope, :prefix)",
            ExpressionAttributeNames={"#scope": "scope"},
            ExpressionAttributeValues={":user": {"S": user_id}, ":prefix": {"S": prefix}},
        )
        return not response.get("Items")
