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


class SessionNotFoundError(Exception):
    """Raised when an update targets a session that does not exist for this user."""


class ChatMetadataRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: str
    user_id: str
    title: str
    project: str | None = None
    status: ChatStatus = ChatStatus.ACTIVE
    agent_type: str | None = None
    model_id: str | None = None
    mode: ChatMode | None = None  # B55: set per chat, changeable mid-chat
    message_count: int = 0
    created_at: datetime
    updated_at: datetime


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class BaseChatMetadataRepository(ABC):
    @abstractmethod
    def create(self, record: ChatMetadataRecord) -> None: ...

    @abstractmethod
    def get(self, user_id: str, session_id: str) -> ChatMetadataRecord | None: ...

    @abstractmethod
    def list_by_user(
        self,
        user_id: str,
        *,
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
    def increment_message_count(self, user_id: str, session_id: str) -> None: ...

    @abstractmethod
    def archive(self, user_id: str, session_id: str) -> None: ...


class ChatMetadataRepository(BaseChatMetadataRepository):

    def __init__(self, session: boto3.Session | None = None) -> None:
        self._session = session or boto3.Session(region_name=config.aws_region)
        self._table = self._session.resource("dynamodb").Table(config.chat_metadata_table)

    def create(self, record: ChatMetadataRecord) -> None:
        self._table.put_item(Item=record.model_dump(mode="json"))

    def get(self, user_id: str, session_id: str) -> ChatMetadataRecord | None:
        response = self._table.get_item(Key={"user_id": user_id, "session_id": session_id})
        item = response.get("Item")
        return ChatMetadataRecord(**from_dynamo(item)) if item else None

    def list_by_user(
        self,
        user_id: str,
        *,
        status: ChatStatus | None = None,
        limit: int = 20,
        start_key: dict[str, Any] | None = None,
    ) -> Page[ChatMetadataRecord]:
        from boto3.dynamodb.conditions import Attr, Key

        kwargs: dict[str, Any] = {
            "KeyConditionExpression": Key("user_id").eq(user_id),
            "ScanIndexForward": False,  # time-sortable session ids: newest first
            "Limit": limit,
        }
        if status is not None:
            kwargs["FilterExpression"] = Attr("status").eq(status.value)
        if start_key is not None:
            kwargs["ExclusiveStartKey"] = start_key

        response = self._table.query(**kwargs)
        items = [ChatMetadataRecord(**from_dynamo(item)) for item in response.get("Items", [])]
        return Page(items=items, next_key=from_dynamo(response.get("LastEvaluatedKey")))

    def _update(
        self, user_id: str, session_id: str, sets: dict[str, Any], add_count: bool = False
    ) -> ChatMetadataRecord:
        names = {"#updated_at": "updated_at"}
        values: dict[str, Any] = {":updated_at": _now()}
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
        try:
            response = self._table.update_item(
                Key={"user_id": user_id, "session_id": session_id},
                UpdateExpression=expression,
                ConditionExpression="attribute_exists(session_id)",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
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
        if mode is not None:
            sets["mode"] = mode.value
        return self._update(user_id, session_id, sets)

    def update_title(self, user_id: str, session_id: str, title: str) -> None:
        self.update(user_id, session_id, title=title)

    def increment_message_count(self, user_id: str, session_id: str) -> None:
        self._update(user_id, session_id, {}, add_count=True)

    def archive(self, user_id: str, session_id: str) -> None:
        self._update(user_id, session_id, {"status": ChatStatus.ARCHIVED.value})
