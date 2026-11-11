"""Account-deletion gate (data-model.md Section 4, B109).

Lives in the UserMetadataTable, keyed by (user_id, record="deletion") -- a second, distinct sort-key
value next to the "profile" settings record (shared/persistence/user_metadata.py). Every route that
starts a new run or writes user data reads this gate first (api.md, F19); `POST /account/delete`
(backend/lambdas/account-delete/handler.py) writes it; the scheduled delete-worker removes it last.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

import boto3
from botocore.exceptions import ClientError
from pydantic import BaseModel, ConfigDict

from shared.config import config
from shared.persistence.chat_metadata import to_iso
from shared.persistence.page import from_dynamo

RECORD_SORT_KEY = "record"
DELETION_RECORD = "deletion"


class AccountDeletionGate(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: str
    requested_at: datetime
    sweep_at: datetime
    # ADR: the Cognito pool "Username" is captured on the gate, not re-derived later | Rationale: this user
    # pool only sets signInAliases.email (AliasAttributes), not usernameAttributes, so the pool-internal
    # Username is not necessarily the Cognito `sub`; the scheduled worker runs 9 hours later with only
    # {"user_id": sub} (no token, no claims) and needs this value for its own AdminDeleteUser call
    cognito_username: str | None = None


def schedule_name(user_id: str) -> str:
    """Deterministic one-time-schedule name for a user (B109): repeat/retry calls are idempotent,
    and the Cognito `sub` is never embedded directly in an AWS resource name."""
    digest = hashlib.sha256(user_id.encode()).hexdigest()[:48]
    return f"account-delete-{digest}"


class AccountGateRepository:
    def __init__(self, session: boto3.Session | None = None) -> None:
        self._session = session or boto3.Session(region_name=config.aws_region)
        self._table = self._session.resource("dynamodb").Table(config.user_metadata_table)

    def write_gate(self, user_id: str, *, requested_at: datetime, sweep_at: datetime,
                   cognito_username: str | None = None) -> AccountDeletionGate:
        """Conditional put so a repeated `POST /account/delete` is idempotent (B109: returns 202 again).

        A retry never moves `sweep_at` later: the existing gate (and its original sweep time) always wins.
        """
        item: dict[str, Any] = {
            "user_id": user_id,
            RECORD_SORT_KEY: DELETION_RECORD,
            "requested_at": to_iso(requested_at),
            "sweep_at": to_iso(sweep_at),
            "cognito_username": cognito_username,
        }
        try:
            self._table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(#r)",
                ExpressionAttributeNames={"#r": RECORD_SORT_KEY},
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            existing = self.get(user_id)
            if existing is not None:
                return existing
            # Deleted between the failed put and this read: write it again, best effort.
            self._table.put_item(Item=item)
        return AccountDeletionGate(
            user_id=user_id, requested_at=requested_at, sweep_at=sweep_at, cognito_username=cognito_username,
        )

    def get(self, user_id: str) -> AccountDeletionGate | None:
        response = self._table.get_item(
            Key={"user_id": user_id, RECORD_SORT_KEY: DELETION_RECORD}, ConsistentRead=True
        )
        item = response.get("Item")
        return AccountDeletionGate(**from_dynamo(item)) if item else None

    def is_deleting(self, user_id: str) -> bool:
        """The run-start gate check (api.md): true once `POST /account/delete` has been accepted."""
        return self.get(user_id) is not None

    def delete_gate(self, user_id: str) -> None:
        self._table.delete_item(Key={"user_id": user_id, RECORD_SORT_KEY: DELETION_RECORD})
