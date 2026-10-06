from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

import boto3
from pydantic import BaseModel, ConfigDict, Field, model_validator

from shared.config import config
from shared.persistence.page import Page, from_dynamo


class MessageRole(StrEnum):
    HUMAN = "human"
    AI = "ai"
    TOOL = "tool"


class ContentBlock(BaseModel):
    model_config = ConfigDict(frozen=True)

    block_type: Literal["text", "ui", "a2ui", "thinking"]
    body: str | dict[str, Any]

    @model_validator(mode="after")
    def validate_ui_body(self) -> ContentBlock:
        if self.block_type == "ui":
            if not isinstance(self.body, dict):
                raise ValueError("ui content blocks require an object body")
            # ADR: lazy, optional schema import | Rationale: the Lambda layer ships only persistence/ and config.py, so
            # read-only handlers skip ui-body validation; agents (full `shared` package) still validate on write
            try:
                from shared.schemas.cei_ui import CEIUISurface
            except ModuleNotFoundError:
                return self
            CEIUISurface.model_validate(self.body)
        return self


class ChatHistoryRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: str
    message_id: str
    role: MessageRole
    content_blocks: list[ContentBlock] = Field(default_factory=list)
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    tool_output: dict[str, Any] | None = None
    citations: list[dict[str, Any]] = Field(default_factory=list)
    timestamp: datetime
    sequence_number: int


TURN_RECORD_TYPE = "turn"


class BaseChatHistoryRepository(ABC):
    @abstractmethod
    def append(self, record: ChatHistoryRecord) -> None: ...

    @abstractmethod
    def load_page(
        self,
        session_id: str,
        *,
        limit: int = 50,
        before_sequence: int | None = None,
        start_key: dict[str, Any] | None = None,
    ) -> Page[ChatHistoryRecord]: ...

    @abstractmethod
    def delete_session(self, session_id: str) -> None: ...


class ChatHistoryRepository(BaseChatHistoryRepository):

    def __init__(self, session: boto3.Session | None = None) -> None:
        self._session = session or boto3.Session(region_name=config.aws_region)
        self._table = self._session.resource("dynamodb").Table(config.chat_history_table)

    def append(self, record: ChatHistoryRecord) -> None:
        self._table.put_item(Item=record.model_dump(mode="json"))

    def load_page(
        self,
        session_id: str,
        *,
        limit: int = 50,
        before_sequence: int | None = None,
        start_key: dict[str, Any] | None = None,
    ) -> Page[ChatHistoryRecord]:
        """Newest turns first across pages; chronological within the returned page (B59).

        Items whose `record_type` is not a turn (F6 trace/SSE items) are skipped, so a page is
        filled by paging on until `limit` turns are found or the partition ends.
        """
        from boto3.dynamodb.conditions import Key

        key_condition = Key("session_id").eq(session_id)
        if before_sequence is not None:
            key_condition &= Key("sequence_number").lt(before_sequence)

        turns: list[dict[str, Any]] = []
        exclusive_start = start_key
        while True:
            kwargs: dict[str, Any] = {
                "KeyConditionExpression": key_condition,
                "ScanIndexForward": False,
                "Limit": limit,
            }
            if exclusive_start is not None:
                kwargs["ExclusiveStartKey"] = exclusive_start
            response = self._table.query(**kwargs)
            turns.extend(
                item
                for item in response.get("Items", [])
                if item.get("record_type", TURN_RECORD_TYPE) == TURN_RECORD_TYPE
            )
            exclusive_start = response.get("LastEvaluatedKey")
            if len(turns) >= limit or exclusive_start is None:
                break

        more = len(turns) > limit or exclusive_start is not None
        turns = turns[:limit]
        next_key: dict[str, Any] | None = None
        if turns and more:
            last = turns[-1]
            next_key = from_dynamo(
                {"session_id": last["session_id"], "sequence_number": last["sequence_number"]}
            )

        records = [ChatHistoryRecord(**from_dynamo(item)) for item in turns]
        records.reverse()
        return Page(items=records, next_key=next_key)

    def delete_session(self, session_id: str) -> None:
        raise NotImplementedError
