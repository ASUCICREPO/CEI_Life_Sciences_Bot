"""Short-term-memory checkpoints stored in the ChatHistoryStore partition of a session.

The latest LangGraph checkpoint of a thread's root namespace (checkpoint_ns="") plus its pending writes (HITL
interrupts, tool results) are kept as special items in the session's own `session_id` partition, in a RESERVED
NEGATIVE sort-key range that never collides with message turns (turns are >= 0):

  sequence_number -1            record_type="checkpoint_head"   (version, chunk_count, serde type, owner)
  sequence_number -2 - i        record_type="checkpoint_chunk"  (i in 0..max_chunks-1, gzip'd serialized bytes)

Only the newest snapshot is kept (upserts). Version = the AI turn's sequence number. Chunks are written first under
the new version, then the head is put conditionally so an older version can never replace a newer one; a reader
accepts a snapshot only if every chunk's version and the chunk count match the head.

LangGraph is imported lazily: the read-only Lambda layer ships `persistence/` without it.
"""
from __future__ import annotations

import gzip
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import boto3
from botocore.exceptions import ClientError

from shared.config import config

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple

log = logging.getLogger(__name__)

CHECKPOINT_HEAD_RECORD_TYPE = "checkpoint_head"
CHECKPOINT_CHUNK_RECORD_TYPE = "checkpoint_chunk"

# Reserved sort-key range for checkpoint items: [CHECKPOINT_RESERVED_SK_MIN, CHECKPOINT_HEAD_SK]. Turns are >= 0.
CHECKPOINT_HEAD_SK = -1
CHECKPOINT_FIRST_CHUNK_SK = -2
CHECKPOINT_RESERVED_SK_MIN = -1_000
# Hard ceiling on chunk slots that fit the reserved range, whatever `checkpoint_max_chunks` is configured to.
CHECKPOINT_MAX_CHUNK_SLOTS = CHECKPOINT_FIRST_CHUNK_SK - CHECKPOINT_RESERVED_SK_MIN + 1

ROOT_NAMESPACE = ""


def chunk_sort_key(index: int) -> int:
    """Sort key of chunk `index` (0-based)."""
    if not 0 <= index < CHECKPOINT_MAX_CHUNK_SLOTS:
        raise ValueError(f"checkpoint chunk index {index} outside the reserved range")
    return CHECKPOINT_FIRST_CHUNK_SK - index


class CheckpointTooLargeError(Exception):
    """Raised by `encode` when a snapshot needs more than `max_chunks` chunks."""


def _default_serde() -> Any:
    # Match the in-process InMemorySaver default: JsonPlusSerializer with the pickle fallback OFF.
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    return JsonPlusSerializer(pickle_fallback=False)


def _root_config(session_id: str, checkpoint_id: str | None = None) -> dict[str, Any]:
    configurable: dict[str, Any] = {"thread_id": session_id, "checkpoint_ns": ROOT_NAMESPACE}
    if checkpoint_id:
        configurable["checkpoint_id"] = checkpoint_id
    return {"configurable": configurable}


def _as_bytes(value: Any) -> bytes:
    # boto3 returns Binary attributes as boto3.dynamodb.types.Binary (exposes `.value`)
    return bytes(getattr(value, "value", value))


class CheckpointRepository:
    """Owner-scoped latest-checkpoint storage inside ChatHistoryStore."""

    def __init__(
        self,
        session: boto3.Session | None = None,
        *,
        table: Any = None,
        serde: Any = None,
        chunk_bytes: int | None = None,
        max_chunks: int | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if table is None:
            sess = session or boto3.Session(region_name=config.aws_region)
            table = sess.resource("dynamodb").Table(config.chat_history_table)
        self._table = table
        self._serde = serde
        self._chunk_bytes = chunk_bytes or config.checkpoint_chunk_bytes
        self._max_chunks = min(max_chunks or config.checkpoint_max_chunks, CHECKPOINT_MAX_CHUNK_SLOTS)
        self._clock = clock or (lambda: datetime.now(UTC))
        if self._chunk_bytes <= 0:
            raise ValueError("checkpoint_chunk_bytes must be positive")

    @property
    def serde(self) -> Any:
        if self._serde is None:
            self._serde = _default_serde()
        return self._serde

    # ---- encoding -------------------------------------------------------------------------------------------

    def encode(self, tup: CheckpointTuple) -> tuple[str, list[bytes]]:
        """Serialize + gzip a checkpoint tuple and split it into chunks. Raises CheckpointTooLargeError."""
        parent = tup.parent_config["configurable"].get("checkpoint_id") if tup.parent_config else None
        payload = {
            "checkpoint": tup.checkpoint,
            "metadata": tup.metadata,
            "parent_checkpoint_id": parent,
            "pending_writes": [list(w) for w in (tup.pending_writes or [])],
        }
        type_tag, raw = self.serde.dumps_typed(payload)
        data = gzip.compress(raw)
        chunks = [data[i : i + self._chunk_bytes] for i in range(0, len(data), self._chunk_bytes)] or [b""]
        if len(chunks) > self._max_chunks:
            raise CheckpointTooLargeError(
                f"{len(data)} compressed bytes need {len(chunks)} chunks (max {self._max_chunks})"
            )
        return type_tag, chunks

    def decode(self, type_tag: str, chunks: list[bytes]) -> dict[str, Any]:
        return self.serde.loads_typed((type_tag, gzip.decompress(b"".join(chunks))))

    # ---- write ----------------------------------------------------------------------------------------------

    def save_from_saver(self, user_id: str, session_id: str, saver: BaseCheckpointSaver, version: int) -> bool:
        """Persist the saver's latest root-namespace checkpoint for `session_id`. False when nothing was written."""
        tup = saver.get_tuple(_root_config(session_id))
        if tup is None:
            return False
        return self.save(user_id, session_id, tup, version)

    def save(self, user_id: str, session_id: str, tup: CheckpointTuple, version: int) -> bool:
        """Upsert the snapshot under `version`. False when skipped (too large, stale version, foreign owner)."""
        if not user_id:
            raise ValueError("user_id is required on checkpoint write")
        try:
            type_tag, chunks = self.encode(tup)
        except CheckpointTooLargeError as exc:
            log.warning(
                "checkpoint: skipped oversized snapshot session=%s version=%s: %s "
                "(metric checkpoint.skipped_oversize=1)",
                session_id,
                version,
                exc,
            )
            return False

        previous = self._table.get_item(
            Key={"session_id": session_id, "sequence_number": CHECKPOINT_HEAD_SK}, ConsistentRead=True
        ).get("Item")
        if previous is not None:
            if previous.get("user_id") != user_id:
                log.warning("checkpoint: head owned by another user, not writing session=%s", session_id)
                return False
            if int(previous.get("version", -1)) >= version:
                log.info("checkpoint: stored version is not older, skipping session=%s v=%s", session_id, version)
                return False

        # 1. chunks under the new version (never over a newer version's chunk, never over a foreign owner's item)
        for index, chunk in enumerate(chunks):
            item = {
                "session_id": session_id,
                "sequence_number": chunk_sort_key(index),
                "record_type": CHECKPOINT_CHUNK_RECORD_TYPE,
                "user_id": user_id,
                "version": version,
                "chunk_index": index,
                "data": chunk,
            }
            if not self._conditional_put(item, version, user_id, allow_equal=True):
                log.info("checkpoint: newer chunk present, abandoning write session=%s v=%s", session_id, version)
                return False

        # 2. head, conditional so an older version can never replace a newer one
        head = {
            "session_id": session_id,
            "sequence_number": CHECKPOINT_HEAD_SK,
            "record_type": CHECKPOINT_HEAD_RECORD_TYPE,
            "user_id": user_id,
            "version": version,
            "chunk_count": len(chunks),
            "serde_type": type_tag,
            "checkpoint_id": tup.checkpoint.get("id"),
            "updated_at": self._clock().isoformat(),
        }
        if not self._conditional_put(head, version, user_id, allow_equal=False):
            log.info("checkpoint: newer head present, write lost the race session=%s v=%s", session_id, version)
            return False

        # 3. best-effort cleanup of stale chunks beyond the new count
        old_count = int(previous.get("chunk_count", 0)) if previous is not None else 0
        for index in range(len(chunks), min(old_count, CHECKPOINT_MAX_CHUNK_SLOTS)):
            try:
                self._table.delete_item(
                    Key={"session_id": session_id, "sequence_number": chunk_sort_key(index)},
                    ConditionExpression="attribute_exists(sequence_number) AND version < :v",
                    ExpressionAttributeValues={":v": version},
                )
            except ClientError as exc:  # stale chunk already gone / rewritten by a newer version
                log.debug("checkpoint: stale chunk %s not deleted session=%s: %s", index, session_id, exc)
            except Exception:  # noqa: BLE001
                log.warning("checkpoint: stale chunk cleanup failed session=%s", session_id, exc_info=True)
        return True

    def _conditional_put(self, item: dict[str, Any], version: int, user_id: str, *, allow_equal: bool) -> bool:
        op = "<=" if allow_equal else "<"
        try:
            self._table.put_item(
                Item=item,
                ConditionExpression=f"attribute_not_exists(sequence_number) OR (version {op} :v AND user_id = :u)",
                ExpressionAttributeValues={":v": version, ":u": user_id},
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise
        return True

    # ---- read -----------------------------------------------------------------------------------------------

    def _reserved_items(self, session_id: str) -> list[dict[str, Any]]:
        from boto3.dynamodb.conditions import Key

        condition = Key("session_id").eq(session_id) & Key("sequence_number").between(
            CHECKPOINT_RESERVED_SK_MIN, CHECKPOINT_HEAD_SK
        )
        items: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {"KeyConditionExpression": condition, "ConsistentRead": True}
        while True:
            response = self._table.query(**kwargs)
            items.extend(response.get("Items", []))
            if not (last := response.get("LastEvaluatedKey")):
                return items
            kwargs["ExclusiveStartKey"] = last

    def load(self, user_id: str, session_id: str) -> dict[str, Any] | None:
        """The decoded snapshot payload, or None if absent / foreign / torn / undecodable."""
        items = {int(item["sequence_number"]): item for item in self._reserved_items(session_id)}
        head = items.get(CHECKPOINT_HEAD_SK)
        if head is None or head.get("record_type") != CHECKPOINT_HEAD_RECORD_TYPE:
            return None
        if head.get("user_id") != user_id:
            log.warning("checkpoint: head owner mismatch, ignoring snapshot session=%s", session_id)
            return None
        version = int(head["version"])
        count = int(head.get("chunk_count", 0))
        if not 0 < count <= CHECKPOINT_MAX_CHUNK_SLOTS:
            return None
        chunks: list[bytes] = []
        for index in range(count):
            chunk = items.get(chunk_sort_key(index))
            if (
                chunk is None
                or chunk.get("record_type") != CHECKPOINT_CHUNK_RECORD_TYPE
                or chunk.get("user_id") != user_id
                or int(chunk.get("version", -1)) != version
            ):
                log.info("checkpoint: torn snapshot (chunk %s) session=%s v=%s", index, session_id, version)
                return None
            chunks.append(_as_bytes(chunk["data"]))
        try:
            payload = self.decode(str(head["serde_type"]), chunks)
        except Exception:  # noqa: BLE001
            log.warning("checkpoint: undecodable snapshot session=%s v=%s", session_id, version, exc_info=True)
            return None
        if not isinstance(payload, dict) or not isinstance(payload.get("checkpoint"), dict):
            return None
        return payload

    def restore(self, user_id: str, session_id: str, saver: BaseCheckpointSaver) -> bool:
        """Load the snapshot into the in-process saver so the graph resumes with full state. False if unusable."""
        payload = self.load(user_id, session_id)
        if payload is None:
            return False
        checkpoint = payload["checkpoint"]
        stored = saver.put(
            _root_config(session_id, payload.get("parent_checkpoint_id")),
            checkpoint,
            payload.get("metadata") or {},
            dict(checkpoint.get("channel_versions") or {}),
        )
        by_task: dict[str, list[tuple[str, Any]]] = {}
        for task_id, channel, value in payload.get("pending_writes") or []:
            by_task.setdefault(task_id, []).append((channel, value))
        for task_id, writes in by_task.items():
            saver.put_writes(stored, writes, task_id)
        return True
