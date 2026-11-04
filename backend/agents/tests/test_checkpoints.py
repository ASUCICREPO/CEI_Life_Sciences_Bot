from __future__ import annotations

import operator
from typing import Annotated, TypedDict

import boto3
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from moto import mock_aws

from shared.config import config
from shared.persistence.chat_history import ChatHistoryRecord, ChatHistoryRepository, ContentBlock
from shared.persistence.checkpoints import (
    CHECKPOINT_HEAD_SK,
    CHECKPOINT_RESERVED_SK_MIN,
    CheckpointRepository,
    chunk_sort_key,
)

SID = "s-1"
USER = "u-1"


@pytest.fixture
def table(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    with mock_aws():
        ddb = boto3.Session(region_name="us-east-1").resource("dynamodb")
        yield ddb.create_table(
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


class State(TypedDict):
    messages: Annotated[list, operator.add]


def _graph(saver):
    def ask(state: State) -> dict:
        answer = interrupt({"question": "pick one"})
        return {"messages": [AIMessage(content=f"picked {answer}")]}

    builder = StateGraph(State)
    builder.add_node("ask", ask)
    builder.add_edge(START, "ask")
    builder.add_edge("ask", END)
    return builder.compile(checkpointer=saver)


def _cfg():
    return {"configurable": {"thread_id": SID}}


def _interrupted_saver():
    saver = InMemorySaver()
    _graph(saver).invoke({"messages": [HumanMessage(content="hi")]}, _cfg())
    return saver


def test_reserved_range_constants():
    assert chunk_sort_key(0) == -2 and CHECKPOINT_HEAD_SK == -1
    assert chunk_sort_key(config.checkpoint_max_chunks - 1) >= CHECKPOINT_RESERVED_SK_MIN


def test_round_trip_resumes_hitl_interrupt(table):
    repo = CheckpointRepository(table=table)
    assert repo.save_from_saver(USER, SID, _interrupted_saver(), version=3)

    fresh = InMemorySaver()
    assert repo.restore(USER, SID, fresh)
    graph = _graph(fresh)
    state = graph.get_state(_cfg())
    assert state.next == ("ask",) and state.interrupts
    result = graph.invoke(Command(resume="A"), _cfg())
    assert [m.content for m in result["messages"]] == ["hi", "picked A"]


def test_owner_mismatch_and_missing_snapshot(table):
    repo = CheckpointRepository(table=table)
    assert not repo.restore(USER, SID, InMemorySaver())
    repo.save_from_saver(USER, SID, _interrupted_saver(), version=1)
    assert not repo.restore("intruder", SID, InMemorySaver())
    assert not repo.save_from_saver("intruder", SID, _interrupted_saver(), version=9)


def test_older_version_never_replaces_newer(table):
    repo = CheckpointRepository(table=table)
    assert repo.save_from_saver(USER, SID, _interrupted_saver(), version=5)
    assert not repo.save_from_saver(USER, SID, _interrupted_saver(), version=4)
    head = table.get_item(Key={"session_id": SID, "sequence_number": -1})["Item"]
    assert int(head["version"]) == 5


def test_torn_snapshot_is_unusable_and_stale_chunks_deleted(table):
    repo = CheckpointRepository(table=table, chunk_bytes=64)
    assert repo.save_from_saver(USER, SID, _interrupted_saver(), version=1)
    count = int(table.get_item(Key={"session_id": SID, "sequence_number": -1})["Item"]["chunk_count"])
    assert count > 1
    # a shrinking rewrite deletes chunks beyond the new count
    big = CheckpointRepository(table=table, chunk_bytes=1_000_000)
    assert big.save_from_saver(USER, SID, _interrupted_saver(), version=2)
    assert "Item" not in table.get_item(Key={"session_id": SID, "sequence_number": chunk_sort_key(1)})
    assert big.restore(USER, SID, InMemorySaver())
    # chunk version drift => unusable
    table.update_item(
        Key={"session_id": SID, "sequence_number": chunk_sort_key(0)},
        UpdateExpression="SET version = :v",
        ExpressionAttributeValues={":v": 1},
    )
    assert not big.restore(USER, SID, InMemorySaver())


def test_oversized_snapshot_is_skipped(table, caplog):
    repo = CheckpointRepository(table=table, chunk_bytes=16, max_chunks=2)
    assert not repo.save_from_saver(USER, SID, _interrupted_saver(), version=1)
    assert "Item" not in table.get_item(Key={"session_id": SID, "sequence_number": -1})
    assert "oversized" in caplog.text


def test_history_pages_never_see_checkpoint_items(table):
    history = ChatHistoryRepository.__new__(ChatHistoryRepository)
    history._table = table
    CheckpointRepository(table=table, chunk_bytes=64).save_from_saver(USER, SID, _interrupted_saver(), version=1)
    assert history.owner_of(SID) == USER
    history.append(
        ChatHistoryRecord(
            session_id=SID,
            message_id="m0",
            role="human",
            user_id=USER,
            content_blocks=[ContentBlock(block_type="text", body="hi")],
            timestamp="2026-01-01T00:00:00Z",
            sequence_number=0,
        )
    )
    page = history.load_page(USER, SID, limit=5)
    assert [r.sequence_number for r in page.items] == [0] and page.next_key is None
    assert history.load_page(USER, SID, limit=5, before_sequence=0).items == []
