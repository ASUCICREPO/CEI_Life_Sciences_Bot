from __future__ import annotations

import gzip
import json

import boto3
import pytest
from moto import mock_aws

from shared.config import config
from shared.persistence.checkpoints import CHECKPOINT_RESERVED_SK_MIN
from shared.persistence.traces import (
    ENCODING_GZIP,
    ENCODING_JSON,
    TRACE_MAX_CHUNKS,
    TraceRepository,
    TraceTooLargeError,
    trace_sort_key,
)

SID = "s-1"
USER = "u-1"
MSG = "m-ai-1"


def _identity(event):
    return dict(event)


@pytest.fixture
def table(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    with mock_aws():
        ddb = boto3.Session(region_name="us-east-1").resource("dynamodb")
        tbl = ddb.create_table(
            TableName=config.chat_history_table,
            KeySchema=[
                {"AttributeName": "session_id", "KeyType": "HASH"},
                {"AttributeName": "sequence_number", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "session_id", "AttributeType": "S"},
                {"AttributeName": "sequence_number", "AttributeType": "N"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        for seq, message_id in ((0, "m-human-1"), (1, MSG)):
            tbl.put_item(Item={"session_id": SID, "sequence_number": seq, "message_id": message_id, "user_id": USER})
        yield tbl


def _events(n: int, label_size: int = 20) -> list[dict]:
    return [{"seq": i, "type": "trace_event", "kind": "tool_call", "label": "x" * label_size} for i in range(n)]


def test_sort_keys_are_below_checkpoint_range_and_disjoint_per_turn():
    assert trace_sort_key(0, 0) == -1_001
    assert trace_sort_key(0, 0) < CHECKPOINT_RESERVED_SK_MIN
    assert trace_sort_key(0, 63) == -1_064
    assert trace_sort_key(1, 0) == -1_065
    with pytest.raises(ValueError):
        trace_sort_key(0, 64)
    with pytest.raises(ValueError):
        trace_sort_key(-1, 0)


def test_small_trace_is_raw_json_single_chunk():
    repo = TraceRepository(table=object(), redactor=_identity)
    encoding, chunks, raw_bytes = repo.encode(_events(3))
    assert encoding == ENCODING_JSON
    assert len(chunks) == 1
    assert json.loads(chunks[0]) == _events(3)
    assert raw_bytes == len(chunks[0])


def test_large_trace_is_gzipped_and_chunked():
    repo = TraceRepository(table=object(), chunk_bytes=100, gzip_threshold_bytes=1_000, redactor=_identity)
    events = [{"seq": i, "label": f"{i:08d}" * 50} for i in range(50)]
    encoding, chunks, raw_bytes = repo.encode(events)
    assert encoding == ENCODING_GZIP
    assert raw_bytes > 1_000
    assert len(chunks) > 1 and all(len(c) <= 100 for c in chunks)
    assert json.loads(gzip.decompress(b"".join(chunks))) == events


def test_default_limits_are_350kb_chunks_and_400kb_gzip_threshold():
    repo = TraceRepository(table=object(), redactor=_identity)
    assert repo._chunk_bytes == 350_000
    assert repo._gzip_threshold == 400_000


def test_too_many_chunks_raises():
    repo = TraceRepository(table=object(), chunk_bytes=1, gzip_threshold_bytes=10**9, redactor=_identity)
    with pytest.raises(TraceTooLargeError):
        repo.encode([{"label": "y" * TRACE_MAX_CHUNKS}])


def test_round_trip_json(table):
    repo = TraceRepository(table=table, redactor=_identity)
    assert repo.save(USER, SID, 1, MSG, _events(4)) is True
    assert repo.load(USER, SID, MSG) == _events(4)
    head = table.get_item(Key={"session_id": SID, "sequence_number": trace_sort_key(1, 0)})["Item"]
    assert head["record_type"] == "trace_head" and head["encoding"] == ENCODING_JSON


def test_round_trip_gzip_multi_chunk(table):
    repo = TraceRepository(table=table, chunk_bytes=200, gzip_threshold_bytes=500, redactor=_identity)
    events = [{"seq": i, "label": f"{i:06d}-" * 40} for i in range(40)]
    assert repo.save(USER, SID, 1, MSG, events) is True
    head = table.get_item(Key={"session_id": SID, "sequence_number": trace_sort_key(1, 0)})["Item"]
    assert head["encoding"] == ENCODING_GZIP and int(head["chunk_count"]) > 1
    assert repo.load(USER, SID, MSG) == events


def test_resave_replaces_trace(table):
    repo = TraceRepository(table=table, chunk_bytes=50, redactor=_identity)
    repo.save(USER, SID, 1, MSG, _events(10))
    repo.save(USER, SID, 1, MSG, _events(2))
    assert repo.load(USER, SID, MSG) == _events(2)


def test_unknown_message_or_missing_trace_is_none(table):
    repo = TraceRepository(table=table, redactor=_identity)
    assert repo.load(USER, SID, "nope") is None
    assert repo.load(USER, SID, "m-human-1") is None


def test_foreign_owner_cannot_read_or_overwrite(table):
    repo = TraceRepository(table=table, redactor=_identity)
    repo.save(USER, SID, 1, MSG, _events(2))
    assert repo.load("u-2", SID, MSG) is None
    assert repo.save("u-2", SID, 1, MSG, _events(5)) is False
    assert repo.load(USER, SID, MSG) == _events(2)


def test_torn_trace_is_rejected(table):
    repo = TraceRepository(table=table, chunk_bytes=50, redactor=_identity)
    repo.save(USER, SID, 1, MSG, _events(10))
    chunk_key = {"session_id": SID, "sequence_number": trace_sort_key(1, 2)}
    table.update_item(Key=chunk_key, UpdateExpression="SET write_id = :w", ExpressionAttributeValues={":w": "other"})
    assert repo.load(USER, SID, MSG) is None
    table.delete_item(Key=chunk_key)
    assert repo.load(USER, SID, MSG) is None


def test_trace_items_are_invisible_to_turn_and_checkpoint_ranges(table):
    from boto3.dynamodb.conditions import Key

    TraceRepository(table=table, redactor=_identity).save(USER, SID, 1, MSG, _events(2))
    turns = table.query(KeyConditionExpression=Key("session_id").eq(SID) & Key("sequence_number").gte(0))["Items"]
    assert {t["message_id"] for t in turns} == {"m-human-1", MSG}
    checkpoints = table.query(
        KeyConditionExpression=Key("session_id").eq(SID) & Key("sequence_number").between(-1_000, -1)
    )["Items"]
    assert checkpoints == []
