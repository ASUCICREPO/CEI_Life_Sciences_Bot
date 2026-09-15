from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from enum import StrEnum
from typing import Any

import boto3
from pydantic import BaseModel, ConfigDict

from shared.config import config


class ChatStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class ChatMetadataRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: str
    user_id: str
    title: str
    project: str | None = None
    status: ChatStatus = ChatStatus.ACTIVE
    agent_type: str | None = None
    model_id: str | None = None
    message_count: int = 0
    created_at: datetime
    updated_at: datetime


class BaseChatMetadataRepository(ABC):
    @abstractmethod
    def create(self, record: ChatMetadataRecord) -> None: ...

    @abstractmethod
    def get(self, session_id: str) -> ChatMetadataRecord | None: ...

    @abstractmethod
    def list_by_user(
        self, user_id: str, *, status: ChatStatus | None = None
    ) -> list[ChatMetadataRecord]: ...

    @abstractmethod
    def update_title(self, session_id: str, title: str) -> None: ...

    @abstractmethod
    def increment_message_count(self, session_id: str) -> None: ...

    @abstractmethod
    def archive(self, session_id: str) -> None: ...


class ChatMetadataRepository(BaseChatMetadataRepository):

    def __init__(self, session: boto3.Session | None = None) -> None:
        self._session = session or boto3.Session(region_name=config.aws_region)
        self._table = self._session.resource("dynamodb").Table(config.chat_metadata_table)

    def create(self, record: ChatMetadataRecord) -> None:
        self._table.put_item(Item=record.model_dump(mode="json"))

    def get(self, session_id: str) -> ChatMetadataRecord | None:
        raise NotImplementedError

    def list_by_user(
        self, user_id: str, *, status: ChatStatus | None = None
    ) -> list[ChatMetadataRecord]:
        from boto3.dynamodb.conditions import Attr, Key

        kwargs: dict[str, Any] = {"KeyConditionExpression": Key("user_id").eq(user_id)}
        if status is not None:
            kwargs["FilterExpression"] = Attr("status").eq(status.value)

        response = self._table.query(**kwargs)
        return [ChatMetadataRecord(**item) for item in response.get("Items", [])]

    def update_title(self, session_id: str, title: str) -> None:
        raise NotImplementedError

    def increment_message_count(self, session_id: str) -> None:
        raise NotImplementedError

    def archive(self, session_id: str) -> None:
        raise NotImplementedError
