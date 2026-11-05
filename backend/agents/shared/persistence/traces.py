"""Per-turn reasoning/tool-call traces stored in the ChatHistoryStore partition of a session (F6, B9/B78).

Traces live in a RESERVED NEGATIVE sort-key range BELOW the checkpoint range ([-1000, -1], checkpoints.py), so
turn pages (sequence_number >= 0) and checkpoint reads (between -1000 and -1) never see them. Each turn owns a
fixed block of TRACE_SLOTS_PER_TURN slots:

  sequence_number = TRACE_SK_TOP - (turn_seq * 64 + 0)      record_type="trace_head"   (message_id, chunk_count,
                                                                                         encoding, write_id, owner)
  sequence_number = TRACE_SK_TOP - (turn_seq * 64 + slot)   record_type="trace_chunk"  (slot 1..63)

Encoding: the redacted event list is serialized as compact JSON; payloads over `trace_gzip_threshold_bytes` are
gzip'd. Either way the bytes are split into chunks of at most `trace_chunk_bytes` (one item cannot hold ~400 KB plus
attributes). Chunks are written first under a fresh `write_id`, then the head; a reader accepts a trace only if the
owner, message id, write id and chunk count all agree, so a torn or foreign trace reads as absent.

Same head/chunk protocol as CheckpointRepository. This module is shipped in the read-only Lambda layer, so the
agent-only redaction module is imported lazily on write.
"""
from __future__ import annotations

import gzip
import json
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import boto3

from shared.config import config
from shared.persistence.chat_history import MIN_TURN_SEQUENCE

log = logging.getLogger(__name__)

TRACE_HEAD_RECORD_TYPE = "trace_head"
TRACE_CHUNK_RECORD_TYPE = "trace_chunk"

# First sort key below the checkpoint reserved range (checkpoints.CHECKPOINT_RESERVED_SK_MIN = -1000).
TRACE_SK_TOP = -1_001
TRACE_SLOTS_PER_TURN = 64
TRACE_MAX_CHUNKS = TRACE_SLOTS_PER_TURN - 1  # slot 0 is the head

ENCODING_JSON = "json"
ENCODING_GZIP = "gzip"

_BATCH_GET_LIMIT = 100


class TraceTooLargeError(Exception):
    """Raised by `encode` when a trace needs more than TRACE_MAX_CHUNKS chunks."""


def trace_sort_key(turn_seq: int, slot: int) -> int:
    """Sort key of `slot` (0 = head, 1..63 = chunks) in the trace block of turn `turn_seq`."""
    if turn_seq < MIN_TURN_SEQUENCE:
        raise ValueError(f"trace turn sequence {turn_seq} must be >= {MIN_TURN_SEQUENCE}")
    if not 0 <= slot < TRACE_SLOTS_PER_TURN:
        raise ValueError(f"trace slot {slot} outside 0..{TRACE_SLOTS_PER_TURN - 1}")
    return TRACE_SK_TOP - (turn_seq * TRACE_SLOTS_PER_TURN + slot)


def _as_bytes(value: Any) -> bytes:
    # boto3 returns Binary attributes as boto3.dynamodb.types.Binary (exposes `.value`)
    return bytes(getattr(value, "value", value))


def _default_redactor() -> Callable[[dict[str, Any]], dict[str, Any]]:
    from shared.streaming.trace_redaction import redact_trace_event

    return redact_trace_event


class TraceRepository:
    """Owner-scoped per-turn trace storage inside ChatHistoryStore."""

    def __init__(
        self,
        session: boto3.Session | None = None,
        *,
        table: Any = None,
        chunk_bytes: int | None = None,
        gzip_threshold_bytes: int | None = None,
        clock: Callable[[], datetime] | None = None,
        redactor: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        if table is None:
            sess = session or boto3.Session(region_name=config.aws_region)
            table = sess.resource("dynamodb").Table(config.chat_history_table)
        self._table = table
        self._chunk_bytes = chunk_bytes or config.trace_chunk_bytes
        self._gzip_threshold = config.trace_gzip_threshold_bytes if gzip_threshold_bytes is None else gzip_threshold_bytes
        self._clock = clock or (lambda: datetime.now(UTC))
        self._redactor = redactor
        if self._chunk_bytes <= 0:
            raise ValueError("trace_chunk_bytes must be positive")

    # ---- encoding -------------------------------------------------------------------------------------------

    def encode(self, events: list[dict[str, Any]]) -> tuple[str, list[bytes], int]:
        """(encoding, chunks, raw_bytes). gzip only above the threshold. Raises TraceTooLargeError."""
        raw = json.dumps(events, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
        encoding, data = (ENCODING_GZIP, gzip.compress(raw)) if len(raw) > self._gzip_threshold else (ENCODING_JSON, raw)
        chunks = [data[i : i + self._chunk_bytes] for i in range(0, len(data), self._chunk_bytes)] or [b""]
        if len(chunks) > TRACE_MAX_CHUNKS:
            raise TraceTooLargeError(f"{len(data)} {encoding} bytes need {len(chunks)} chunks (max {TRACE_MAX_CHUNKS})")
        return encoding, chunks, len(raw)

    @staticmethod
    def decode(encoding: str, chunks: list[bytes]) -> list[dict[str, Any]]:
        data = b"".join(chunks)
        if encoding == ENCODING_GZIP:
            data = gzip.decompress(data)
        elif encoding != ENCODING_JSON:
            raise ValueError(f"unknown trace encoding {encoding!r}")
        events = json.loads(data.decode("utf-8"))
        if not isinstance(events, list):
            raise ValueError("trace payload is not a list")
        return events

    # ---- write ----------------------------------------------------------------------------------------------

    def save(
        self, user_id: str, session_id: str, turn_seq: int, message_id: str, events: list[dict[str, Any]]
    ) -> bool:
        """Persist the REDACTED trace of one turn. False when skipped (too large, foreign owner)."""
        if not user_id:
            raise ValueError("user_id is required on trace write")
        redact = self._redactor or _default_redactor()
        # Second redaction layer (B7): whatever the caller passed, nothing un-redacted is persisted.
        safe_events = [redact(event) for event in events]
        try:
            encoding, chunks, raw_bytes = self.encode(safe_events)
        except TraceTooLargeError as exc:
            log.warning("trace: skipped oversized trace session=%s turn=%s: %s", session_id, turn_seq, exc)
            return False

        write_id = uuid.uuid4().hex
        for index, chunk in enumerate(chunks, start=1):
            item = {
                "session_id": session_id,
                "sequence_number": trace_sort_key(turn_seq, index),
                "record_type": TRACE_CHUNK_RECORD_TYPE,
                "user_id": user_id,
                "version": turn_seq,
                "write_id": write_id,
                "chunk_index": index - 1,
                "data": chunk,
            }
            if not self._owner_put(item, user_id):
                log.warning("trace: chunk slot owned by another user, not writing session=%s", session_id)
                return False

        head = {
            "session_id": session_id,
            "sequence_number": trace_sort_key(turn_seq, 0),
            "record_type": TRACE_HEAD_RECORD_TYPE,
            "user_id": user_id,
            "version": turn_seq,
            "write_id": write_id,
            "message_id": message_id,
            "chunk_count": len(chunks),
            "encoding": encoding,
            "raw_bytes": raw_bytes,
            "event_count": len(safe_events),
            "updated_at": self._clock().isoformat(),
        }
        if not self._owner_put(head, user_id):
            log.warning("trace: head owned by another user, not writing session=%s", session_id)
            return False
        return True

    def _owner_put(self, item: dict[str, Any], user_id: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(sequence_number) OR user_id = :u",
                ExpressionAttributeValues={":u": user_id},
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise
        return True

    # ---- read -----------------------------------------------------------------------------------------------

    def find_turn_sequence(self, user_id: str, session_id: str, message_id: str) -> int | None:
        """Sequence number of the caller's turn `message_id`, or None."""
        from boto3.dynamodb.conditions import Attr, Key

        kwargs: dict[str, Any] = {
            "KeyConditionExpression": Key("session_id").eq(session_id) & Key("sequence_number").gte(MIN_TURN_SEQUENCE),
            "FilterExpression": Attr("message_id").eq(message_id),
            "ProjectionExpression": "sequence_number, message_id, user_id",
        }
        while True:
            response = self._table.query(**kwargs)
            for item in response.get("Items", []):
                if item.get("user_id") not in (None, user_id):
                    continue
                return int(item["sequence_number"])
            if not (last := response.get("LastEvaluatedKey")):
                return None
            kwargs["ExclusiveStartKey"] = last

    def load(self, user_id: str, session_id: str, message_id: str) -> list[dict[str, Any]] | None:
        """The decoded event list, or None if absent / foreign / torn / undecodable."""
        turn_seq = self.find_turn_sequence(user_id, session_id, message_id)
        if turn_seq is None:
            return None
        head = self._table.get_item(
            Key={"session_id": session_id, "sequence_number": trace_sort_key(turn_seq, 0)}, ConsistentRead=True
        ).get("Item")
        if head is None or head.get("record_type") != TRACE_HEAD_RECORD_TYPE:
            return None
        if head.get("user_id") != user_id or head.get("message_id") != message_id:
            log.warning("trace: head owner/message mismatch, ignoring session=%s", session_id)
            return None
        count = int(head.get("chunk_count", 0))
        if not 0 < count <= TRACE_MAX_CHUNKS:
            return None
        items = self._batch_get(session_id, [trace_sort_key(turn_seq, slot) for slot in range(1, count + 1)])
        chunks: list[bytes] = []
        for index in range(count):
            chunk = items.get(trace_sort_key(turn_seq, index + 1))
            if (
                chunk is None
                or chunk.get("record_type") != TRACE_CHUNK_RECORD_TYPE
                or chunk.get("user_id") != user_id
                or chunk.get("write_id") != head.get("write_id")
                or int(chunk.get("version", -1)) != int(head["version"])
            ):
                log.info("trace: torn trace (chunk %s) session=%s turn=%s", index, session_id, turn_seq)
                return None
            chunks.append(_as_bytes(chunk["data"]))
        try:
            return self.decode(str(head.get("encoding", ENCODING_JSON)), chunks)
        except Exception:  # noqa: BLE001
            log.warning("trace: undecodable trace session=%s turn=%s", session_id, turn_seq, exc_info=True)
            return None

    def _batch_get(self, session_id: str, sort_keys: list[int]) -> dict[int, dict[str, Any]]:
        client = self._table.meta.client
        name = self._table.name
        found: dict[int, dict[str, Any]] = {}
        for start in range(0, len(sort_keys), _BATCH_GET_LIMIT):
            request: dict[str, Any] = {
                name: {
                    "Keys": [{"session_id": session_id, "sequence_number": sk} for sk in sort_keys[start : start + _BATCH_GET_LIMIT]],
                    "ConsistentRead": True,
                }
            }
            for _attempt in range(5):
                response = client.batch_get_item(RequestItems=request)
                for item in response.get("Responses", {}).get(name, []):
                    found[int(item["sequence_number"])] = item
                request = response.get("UnprocessedKeys") or {}
                if not request:
                    break
        return found
