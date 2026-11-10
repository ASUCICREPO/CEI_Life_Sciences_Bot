from __future__ import annotations

import json
from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

import boto3
from botocore.exceptions import ClientError
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
    """One DynamoDB item per message (PK session_id, SK sequence_number)."""

    model_config = ConfigDict(frozen=True)

    session_id: str
    message_id: str
    role: MessageRole
    # Owner (Cognito sub). Required on write; optional on read so pre-owner legacy items still load.
    user_id: str | None = None
    content_blocks: list[ContentBlock] = Field(default_factory=list)
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    tool_output: dict[str, Any] | None = None
    citations: list[dict[str, Any]] = Field(default_factory=list)
    timestamp: datetime
    sequence_number: int
    truncated: bool = False  # set when the size guard trimmed this item


TURN_RECORD_TYPE = "turn"
# Lowest sort key a turn may use; negative sort keys are reserved for checkpoint items ([-1000, -1],
# shared/persistence/checkpoints.py) and per-turn trace items (<= -1001, shared/persistence/traces.py).
MIN_TURN_SEQUENCE = 0

# DynamoDB's hard item limit is 400 KB; leave headroom for attribute-name overhead and encoding drift.
MAX_ITEM_BYTES = 350_000
TRUNCATION_MARKER = "\n\n…[truncated]"


class SequenceConflictError(Exception):
    """Raised when `append` targets a (session_id, sequence_number) that is already taken."""


class HistoryItemTooLargeError(Exception):
    """Raised when an item is still over MAX_ITEM_BYTES after every trimmable field was trimmed."""


def _item_size(item: dict[str, Any]) -> int:
    return len(json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def fit_item(item: dict[str, Any], max_bytes: int = MAX_ITEM_BYTES) -> dict[str, Any]:
    """Shrink a serialized history item until it fits, in order of least user-visible loss.

    1. drop `tool_output`  2. drop `thinking` blocks  3. cut the longest text blocks.
    `ui` / `a2ui` blocks are never cut (a partial CEI surface would fail schema validation on read).
    """
    if _item_size(item) <= max_bytes:
        return item
    item = {**item, "content_blocks": [dict(b) for b in item.get("content_blocks", [])], "truncated": True}

    if item.get("tool_output") is not None:
        item["tool_output"] = None
    if _item_size(item) > max_bytes:
        item["content_blocks"] = [b for b in item["content_blocks"] if b.get("block_type") != "thinking"]

    while (overflow := _item_size(item) - max_bytes) > 0:
        texts = [b for b in item["content_blocks"] if b.get("block_type") == "text" and isinstance(b.get("body"), str)]
        target = max(texts, key=lambda b: len(b["body"].encode("utf-8")), default=None)
        if target is None or len(target["body"]) <= len(TRUNCATION_MARKER):
            raise HistoryItemTooLargeError(
                f"history item {item.get('session_id')}#{item.get('sequence_number')} is still "
                f"{overflow} bytes over the limit after trimming"
            )
        body: str = target["body"]
        # cut by at least the overflow (bytes >= chars, so cutting chars by the byte overflow is enough)
        keep = max(0, len(body) - overflow - len(TRUNCATION_MARKER) - 64)
        target["body"] = body[:keep] + TRUNCATION_MARKER
    return item


class BaseChatHistoryRepository(ABC):
    @abstractmethod
    def append(self, record: ChatHistoryRecord) -> None: ...

    @abstractmethod
    def load_page(
        self,
        user_id: str,
        session_id: str,
        *,
        limit: int = 50,
        before_sequence: int | None = None,
        start_key: dict[str, Any] | None = None,
    ) -> Page[ChatHistoryRecord]: ...

    @abstractmethod
    def load_recent(self, user_id: str, session_id: str, n_turns: int = 20) -> list[ChatHistoryRecord]: ...

    @abstractmethod
    def owner_of(self, session_id: str) -> str | None: ...

    @abstractmethod
    def delete_session(self, session_id: str) -> None: ...


class ChatHistoryRepository(BaseChatHistoryRepository):
    """Owner-scoped access to ChatHistoryStore.

    App-layer RLS: callers must first prove ownership via the metadata row (user_id, session_id); every item also
    carries `user_id`, and reads drop items stamped with a different owner (defence in depth).
    """

    def __init__(self, session: boto3.Session | None = None) -> None:
        self._session = session or boto3.Session(region_name=config.aws_region)
        self._table = self._session.resource("dynamodb").Table(config.chat_history_table)

    def append(self, record: ChatHistoryRecord) -> None:
        """Write one message. Never overwrites an existing sequence number."""
        if not record.user_id:
            raise ValueError("ChatHistoryRecord.user_id is required on write")
        item = fit_item(record.model_dump(mode="json"))
        # boto3's resource layer rejects Python floats (chart data, scores); round-trip them to Decimal
        item = json.loads(json.dumps(item), parse_float=Decimal)
        try:
            self._table.put_item(Item=item, ConditionExpression="attribute_not_exists(sequence_number)")
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise SequenceConflictError(f"{record.session_id}#{record.sequence_number}") from exc
            raise

    def load_page(
        self,
        user_id: str,
        session_id: str,
        *,
        limit: int = 50,
        before_sequence: int | None = None,
        start_key: dict[str, Any] | None = None,
    ) -> Page[ChatHistoryRecord]:
        """Newest turns first across pages; chronological within the returned page (B59).

        Items whose `record_type` is not a turn (F6 trace/SSE items), and items stamped with another owner, are
        skipped, so a page is filled by paging on until `limit` turns are found or the partition ends.
        """
        from boto3.dynamodb.conditions import Key

        # Turns live at sequence_number >= 0; the negative range is reserved for checkpoint items (checkpoints.py),
        # so bound the key condition instead of reading (and filtering out) up to a page of checkpoint chunks.
        if before_sequence is not None:
            if before_sequence <= MIN_TURN_SEQUENCE:
                return Page(items=[], next_key=None)
            key_condition = Key("session_id").eq(session_id) & Key("sequence_number").between(
                MIN_TURN_SEQUENCE, before_sequence - 1
            )
        else:
            key_condition = Key("session_id").eq(session_id) & Key("sequence_number").gte(MIN_TURN_SEQUENCE)

        def visible(item: dict[str, Any]) -> bool:
            if item.get("record_type", TURN_RECORD_TYPE) != TURN_RECORD_TYPE:
                return False
            owner = item.get("user_id")
            return owner is None or owner == user_id

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
            turns.extend(item for item in response.get("Items", []) if visible(item))
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

    def load_recent(self, user_id: str, session_id: str, n_turns: int = 20) -> list[ChatHistoryRecord]:
        """The last `n_turns` messages, oldest first (for rehydrating an agent's short-term memory)."""
        if n_turns <= 0:
            return []
        return self.load_page(user_id, session_id, limit=n_turns).items

    def owner_of(self, session_id: str) -> str | None:
        """`user_id` stamped on the first item of a session, or None if empty/legacy.

        Used on the write path: the metadata row is keyed by (user_id, session_id), so it cannot reveal that a
        colliding session_id already belongs to someone else — the history partition can.
        """
        response = self._table.query(
            KeyConditionExpression="session_id = :sid",
            ExpressionAttributeValues={":sid": session_id},
            ProjectionExpression="user_id",
            ScanIndexForward=True,
            Limit=1,
        )
        items = response.get("Items", [])
        return items[0].get("user_id") if items else None

    def delete_session(self, session_id: str) -> None:
        """Delete all items in a session partition after the caller verifies ownership.

        Includes negative sequence numbers used by checkpoints and trace chunks.
        Replay partitions have separate keys and must be removed by the deletion worker.
        """
        if not session_id:
            raise ValueError("session_id is required")
        start_key = None
        while True:
            args: dict[str, Any] = {
                "KeyConditionExpression": "session_id = :sid",
                "ExpressionAttributeValues": {":sid": session_id},
                "ProjectionExpression": "session_id, sequence_number",
                "ConsistentRead": True,
            }
            if start_key is not None:
                args["ExclusiveStartKey"] = start_key
            page = self._table.query(**args)
            with self._table.batch_writer() as batch:
                for item in page.get("Items", []):
                    batch.delete_item(Key={"session_id": item["session_id"], "sequence_number": item["sequence_number"]})
            start_key = page.get("LastEvaluatedKey")
            if not start_key:
                return
