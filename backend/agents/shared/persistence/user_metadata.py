"""User Metadata Table (data-model.md Section 4): per-user default model/mode settings.

PK `user_id` (Cognito `sub`), SK `record` fixed to `"profile"` (single-item-per-user table).
First real use of this table (F8). B76: no per-user model restriction field here — only the
global blocklist exists; F18 extends this module for `configured_sources`/API-key references,
coordinate through this module rather than duplicating it.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import boto3
from pydantic import BaseModel, ConfigDict

from shared.config import config
from shared.persistence.chat_metadata import ChatMode, to_iso
from shared.persistence.page import from_dynamo

RECORD_SORT_KEY = "record"
_PROFILE_RECORD = "profile"


class UserMetadataRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: str
    default_model_id: str | None = None
    default_mode: ChatMode | None = None
    created_at: datetime
    updated_at: datetime


class UserMetadataRepository:
    def __init__(self, session: boto3.Session | None = None) -> None:
        self._session = session or boto3.Session(region_name=config.aws_region)
        self._table = self._session.resource("dynamodb").Table(config.user_metadata_table)

    def get(self, user_id: str) -> UserMetadataRecord | None:
        response = self._table.get_item(
            Key={"user_id": user_id, RECORD_SORT_KEY: _PROFILE_RECORD}, ConsistentRead=True
        )
        item = response.get("Item")
        return UserMetadataRecord(**from_dynamo(item)) if item else None

    def put(self, user_id: str, *, default_model_id: str | None,
            default_mode: ChatMode | None) -> UserMetadataRecord:
        """Full replace (PUT /settings semantics): both fields are set to exactly what is given."""
        now = datetime.now(UTC)
        existing = self.get(user_id)
        created_at = existing.created_at if existing is not None else now
        item: dict[str, Any] = {
            "user_id": user_id,
            RECORD_SORT_KEY: _PROFILE_RECORD,
            "default_model_id": default_model_id,
            "default_mode": default_mode.value if default_mode is not None else None,
            "created_at": to_iso(created_at),
            "updated_at": to_iso(now),
        }
        self._table.put_item(Item=item)
        return UserMetadataRecord(
            user_id=user_id, default_model_id=default_model_id, default_mode=default_mode,
            created_at=created_at, updated_at=now,
        )
