from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import boto3
from botocore.exceptions import ClientError
from pydantic import BaseModel, ConfigDict

from shared.config import config
from shared.persistence.page import Page, from_dynamo


class ChatStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class ChatMode(StrEnum):
    SCIENCE = "science"
    BUSINESS = "business"


class SortKey(StrEnum):
    """History-list ordering; each value is backed by a (user_id, <timestamp>) GSI."""

    CREATED = "created"
    UPDATED = "updated"


# GSI names must match backend/lib/backend-stack.ts (ChatMetadataStore indexes)
SORT_INDEX: dict[SortKey, str] = {SortKey.CREATED: "by_created", SortKey.UPDATED: "by_updated"}
SORT_ATTRIBUTE: dict[SortKey, str] = {SortKey.CREATED: "created_at", SortKey.UPDATED: "updated_at"}


class TitleSource(StrEnum):
    """Where a chat title came from. Only a `prompt` title may be replaced by a generated one."""

    PROMPT = "prompt"
    GENERATED = "generated"
    USER = "user"


class SessionNotFoundError(Exception):
    """Raised when an update targets a session that does not exist for this user."""


class ChatMetadataRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: str
    user_id: str
    title: str
    title_source: TitleSource = TitleSource.PROMPT  # default keeps legacy rows loadable
    project: str | None = None
    status: ChatStatus = ChatStatus.ACTIVE
    agent_type: str | None = None
    model_id: str | None = None
    mode: ChatMode | None = None  # B55: set per chat, changeable mid-chat
    document_type: str | None = None  # drafting runtime: needed to resume a drafting chat
    analysis_type: str | None = None  # analysis runtime: selects the analysis task on resume
    message_count: int = 0
    created_at: datetime
    updated_at: datetime


def to_iso(value: datetime) -> str:
    """Fixed-width UTC ISO-8601 with microseconds and a 'Z' suffix.

    The by_created / by_updated GSIs sort these strings lexicographically, so every write must use exactly
    this shape: `isoformat()` drops the fraction when microseconds == 0 ('...00Z' sorts after '...00.5Z'),
    and naive or non-UTC datetimes would sort by wall-clock text.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _now() -> str:
    return to_iso(datetime.now(UTC))


def _is_conditional_failure(exc: ClientError) -> bool:
    return exc.response["Error"]["Code"] == "ConditionalCheckFailedException"


class BaseChatMetadataRepository(ABC):
    @abstractmethod
    def create(self, record: ChatMetadataRecord) -> None: ...

    @abstractmethod
    def get_or_create(self, record: ChatMetadataRecord) -> ChatMetadataRecord: ...

    @abstractmethod
    def get(self, user_id: str, session_id: str) -> ChatMetadataRecord | None: ...

    @abstractmethod
    def list_by_user(
        self,
        user_id: str,
        *,
        sort: SortKey = SortKey.UPDATED,
        status: ChatStatus | None = None,
        limit: int = 20,
        start_key: dict[str, Any] | None = None,
    ) -> Page[ChatMetadataRecord]: ...

    @abstractmethod
    def update(
        self,
        user_id: str,
        session_id: str,
        *,
        title: str | None = None,
        mode: ChatMode | None = None,
    ) -> ChatMetadataRecord: ...

    @abstractmethod
    def update_title(self, user_id: str, session_id: str, title: str) -> None: ...

    @abstractmethod
    def set_generated_title(self, user_id: str, session_id: str, title: str) -> bool: ...

    @abstractmethod
    def increment_message_count(self, user_id: str, session_id: str) -> int: ...

    @abstractmethod
    def archive(self, user_id: str, session_id: str) -> None: ...


class ChatMetadataRepository(BaseChatMetadataRepository):

    def __init__(self, session: boto3.Session | None = None) -> None:
        self._session = session or boto3.Session(region_name=config.aws_region)
        self._table = self._session.resource("dynamodb").Table(config.chat_metadata_table)

    @staticmethod
    def _item(record: ChatMetadataRecord) -> dict[str, Any]:
        item = record.model_dump(mode="json")
        item["created_at"] = to_iso(record.created_at)
        item["updated_at"] = to_iso(record.updated_at)
        return item

    def create(self, record: ChatMetadataRecord) -> None:
        self._table.put_item(Item=self._item(record))

    def get_or_create(self, record: ChatMetadataRecord) -> ChatMetadataRecord:
        """Insert `record` unless (user_id, session_id) already exists; return the stored row either way."""
        try:
            self._table.put_item(
                Item=self._item(record),
                ConditionExpression="attribute_not_exists(session_id)",
            )
            return record
        except ClientError as exc:
            if not _is_conditional_failure(exc):
                raise
        existing = self.get(record.user_id, record.session_id)
        if existing is None:  # deleted between the put and the get
            raise SessionNotFoundError(record.session_id)
        return existing

    def get(self, user_id: str, session_id: str) -> ChatMetadataRecord | None:
        response = self._table.get_item(Key={"user_id": user_id, "session_id": session_id})
        item = response.get("Item")
        return ChatMetadataRecord(**from_dynamo(item)) if item else None

    def list_by_user(
        self,
        user_id: str,
        *,
        sort: SortKey = SortKey.UPDATED,
        status: ChatStatus | None = None,
        limit: int = 20,
        start_key: dict[str, Any] | None = None,
    ) -> Page[ChatMetadataRecord]:
        from boto3.dynamodb.conditions import Attr, Key

        kwargs: dict[str, Any] = {
            "IndexName": SORT_INDEX[SortKey(sort)],
            "KeyConditionExpression": Key("user_id").eq(user_id),
            "ScanIndexForward": False,  # newest first
            "Limit": limit,
        }
        if status is not None:
            kwargs["FilterExpression"] = Attr("status").eq(status.value)
        if start_key is not None:
            kwargs["ExclusiveStartKey"] = start_key

        response = self._table.query(**kwargs)
        items = [ChatMetadataRecord(**from_dynamo(item)) for item in response.get("Items", [])]
        # A GSI cursor is {user_id, session_id, <index sort attribute>}
        return Page(items=items, next_key=from_dynamo(response.get("LastEvaluatedKey")))

    def _update(
        self,
        user_id: str,
        session_id: str,
        sets: dict[str, Any],
        add_count: bool = False,
        condition: str | None = None,
        condition_values: dict[str, Any] | None = None,
    ) -> ChatMetadataRecord:
        names = {"#updated_at": "updated_at"}
        values: dict[str, Any] = {":updated_at": _now(), **(condition_values or {})}
        parts = ["#updated_at = :updated_at"]
        for attr, value in sets.items():
            names[f"#{attr}"] = attr
            values[f":{attr}"] = value
            parts.append(f"#{attr} = :{attr}")
        expression = "SET " + ", ".join(parts)
        if add_count:
            names["#message_count"] = "message_count"
            values[":one"] = 1
            expression += " ADD #message_count :one"
        condition_expression = "attribute_exists(session_id)"
        if condition:
            condition_expression += f" AND ({condition})"
        try:
            response = self._table.update_item(
                Key={"user_id": user_id, "session_id": session_id},
                UpdateExpression=expression,
                ConditionExpression=condition_expression,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if _is_conditional_failure(exc):
                raise SessionNotFoundError(session_id) from exc
            raise
        return ChatMetadataRecord(**from_dynamo(response["Attributes"]))

    def update(
        self,
        user_id: str,
        session_id: str,
        *,
        title: str | None = None,
        mode: ChatMode | None = None,
    ) -> ChatMetadataRecord:
        sets: dict[str, Any] = {}
        if title is not None:
            sets["title"] = title
            sets["title_source"] = TitleSource.USER.value  # a rename is never overwritten by a generated title
        if mode is not None:
            sets["mode"] = mode.value
        return self._update(user_id, session_id, sets)

    def update_title(self, user_id: str, session_id: str, title: str) -> None:
        self.update(user_id, session_id, title=title)

    def set_generated_title(self, user_id: str, session_id: str, title: str) -> bool:
        """Replace a provisional prompt title. Returns False (no write) if the title is no longer `prompt`."""
        try:
            self._update(
                user_id,
                session_id,
                {"title": title, "title_source": TitleSource.GENERATED.value},
                condition="attribute_not_exists(title_source) OR title_source = :prompt_source",
                condition_values={":prompt_source": TitleSource.PROMPT.value},
            )
        except SessionNotFoundError:
            return False
        return True

    def increment_message_count(self, user_id: str, session_id: str) -> int:
        """Atomically bump message_count (and updated_at); the new count is the next sequence number."""
        return self._update(user_id, session_id, {}, add_count=True).message_count

    def archive(self, user_id: str, session_id: str) -> None:
        self._update(user_id, session_id, {"status": ChatStatus.ARCHIVED.value})
