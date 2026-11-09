"""Leases for the two concurrent real-time runs allowed to each user.

All slots live in one user-keyed item. A conditional write makes acquisition atomic
even when separate runners race. Expired leases can be replaced after a crash; a
late cleanup cannot remove a newer run because release checks its unique run id.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from botocore.exceptions import ClientError


class ConcurrencyLimitError(Exception):
    """Both real-time slots are in use, or this session is already running."""


@dataclass(frozen=True)
class RunLease:
    user_id: str
    session_id: str
    run_id: str
    slot: int
    expires_at: int
    scopes: tuple[str, ...]


class ActiveRunRepository:
    """Repository over a DynamoDB table keyed by ``user_id`` and ``record``."""

    def __init__(self, table: Any, *, lease_seconds: int = 960) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self.table = table
        self.lease_seconds = lease_seconds

    def acquire(self, user_id: str, session_id: str, run_id: str, scopes: tuple[str, ...]) -> RunLease:
        if not user_id or not session_id or not run_id or not scopes or len(set(scopes)) != len(scopes):
            raise ValueError("user, session, run and distinct scopes are required")
        now = int(time.time())
        expires = now + self.lease_seconds
        key = {"user_id": user_id, "record": "realtime"}
        for slot in (0, 1):
            other = 1 - slot
            lease = {"session_id": session_id, "run_id": run_id, "expires_at": expires, "scopes": list(scopes)}
            try:
                self.table.update_item(
                    Key=key,
                    UpdateExpression="SET #slot = :lease",
                    ConditionExpression=(
                        "(attribute_not_exists(#slot) OR #slot.#expires <= :now) AND "
                        "(attribute_not_exists(#other) OR #other.#expires <= :now OR #other.#session <> :session)"
                    ),
                    ExpressionAttributeNames={"#slot": f"slot_{slot}", "#other": f"slot_{other}",
                                              "#expires": "expires_at", "#session": "session_id"},
                    ExpressionAttributeValues={":lease": lease, ":now": now, ":session": session_id},
                )
                return RunLease(user_id, session_id, run_id, slot, expires, scopes)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                    raise
        raise ConcurrencyLimitError("No real-time run slot is available")

    def release(self, lease: RunLease) -> bool:
        try:
            self.table.update_item(
                Key={"user_id": lease.user_id, "record": "realtime"},
                UpdateExpression="REMOVE #slot",
                ConditionExpression="#slot.#run = :run",
                ExpressionAttributeNames={"#slot": f"slot_{lease.slot}", "#run": "run_id"},
                ExpressionAttributeValues={":run": lease.run_id},
            )
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise

    def find(self, user_id: str, session_id: str) -> RunLease | None:
        item = self.table.get_item(Key={"user_id": user_id, "record": "realtime"}, ConsistentRead=True).get("Item", {})
        now = int(time.time())
        for slot in (0, 1):
            value = item.get(f"slot_{slot}")
            if isinstance(value, dict) and value.get("session_id") == session_id and value.get("expires_at", 0) > now:
                return RunLease(user_id, session_id, value["run_id"], slot, int(value["expires_at"]), tuple(value["scopes"]))
        return None
