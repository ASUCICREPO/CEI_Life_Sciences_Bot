from __future__ import annotations

from datetime import UTC, datetime

import boto3
import pytest
from moto import mock_aws

from shared.config import config
from shared.persistence.chat_history import ChatHistoryRecord, ChatHistoryRepository, ContentBlock
from shared.persistence.chat_metadata import (
    ChatMetadataRecord,
    ChatMetadataRepository,
    ChatMode,
    ChatStatus,
    SessionNotFoundError,
)

NOW = datetime(2026, 10, 1, tzinfo=UTC)


@pytest.fixture
def session(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    with mock_aws():
        sess = boto3.Session(region_name="us-east-1")
        ddb = sess.resource("dynamodb")
        ddb.create_table(
            TableName=config.chat_metadata_table,
            KeySchema=[
                {"AttributeName": "user_id", "KeyType": "HASH"},
                {"AttributeName": "session_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "user_id", "AttributeType": "S"},
                {"AttributeName": "session_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.create_table(
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
        yield sess


def _meta(user_id="u1", session_id="s1", **kw) -> ChatMetadataRecord:
    return ChatMetadataRecord(
        session_id=session_id, user_id=user_id, title="t", created_at=NOW, updated_at=NOW, **kw
    )


def _turn(session_id: str, seq: int) -> ChatHistoryRecord:
    return ChatHistoryRecord(
        session_id=session_id,
        message_id=f"m{seq}",
        role="human",
        content_blocks=[ContentBlock(block_type="text", body=f"hi {seq}")],
        timestamp=NOW,
        sequence_number=seq,
    )


def test_get_returns_record_scoped_to_user(session):
    repo = ChatMetadataRepository(session)
    repo.create(_meta(mode=ChatMode.SCIENCE))
    got = repo.get("u1", "s1")
    assert got is not None and got.mode == ChatMode.SCIENCE
    assert repo.get("u2", "s1") is None
    assert repo.get("u1", "missing") is None


def test_update_title_and_mode(session):
    repo = ChatMetadataRepository(session)
    repo.create(_meta(mode=ChatMode.SCIENCE))
    repo.update_title("u1", "s1", "renamed")
    updated = repo.update("u1", "s1", mode=ChatMode.BUSINESS)
    assert updated.title == "renamed"
    assert updated.mode == ChatMode.BUSINESS
    assert updated.updated_at > NOW


def test_update_missing_session_raises_and_does_not_upsert(session):
    repo = ChatMetadataRepository(session)
    with pytest.raises(SessionNotFoundError):
        repo.update_title("u1", "nope", "x")
    assert repo.get("u1", "nope") is None


def test_increment_message_count(session):
    repo = ChatMetadataRepository(session)
    repo.create(_meta())
    repo.increment_message_count("u1", "s1")
    repo.increment_message_count("u1", "s1")
    assert repo.get("u1", "s1").message_count == 2


def test_archive_and_status_filter(session):
    repo = ChatMetadataRepository(session)
    repo.create(_meta(session_id="a"))
    repo.create(_meta(session_id="b"))
    repo.archive("u1", "a")
    assert repo.get("u1", "a").status == ChatStatus.ARCHIVED
    active = repo.list_by_user("u1", status=ChatStatus.ACTIVE)
    assert [r.session_id for r in active.items] == ["b"]


def test_list_by_user_pages_newest_first_and_isolates_users(session):
    repo = ChatMetadataRepository(session)
    for i in range(5):
        repo.create(_meta(session_id=f"s{i}"))
    repo.create(_meta(user_id="u2", session_id="other"))
    first = repo.list_by_user("u1", limit=2)
    assert [r.session_id for r in first.items] == ["s4", "s3"]
    assert first.next_key == {"user_id": "u1", "session_id": "s3"}
    second = repo.list_by_user("u1", limit=10, start_key=first.next_key)
    assert [r.session_id for r in second.items] == ["s2", "s1", "s0"]
    assert second.next_key is None


def test_load_page_cursor_and_chronological_order(session):
    repo = ChatHistoryRepository(session)
    for seq in range(1, 6):
        repo.append(_turn("s1", seq))
    page1 = repo.load_page("s1", limit=2)
    assert [r.sequence_number for r in page1.items] == [4, 5]
    assert page1.next_key == {"session_id": "s1", "sequence_number": 4}
    page2 = repo.load_page("s1", limit=2, start_key=page1.next_key)
    assert [r.sequence_number for r in page2.items] == [2, 3]
    page3 = repo.load_page("s1", limit=2, start_key=page2.next_key)
    assert [r.sequence_number for r in page3.items] == [1]
    assert page3.next_key is None


def test_load_page_empty_session(session):
    page = ChatHistoryRepository(session).load_page("none")
    assert page.items == [] and page.next_key is None


def test_load_page_skips_non_turn_records_and_still_fills_page(session):
    repo = ChatHistoryRepository(session)
    table = session.resource("dynamodb").Table(config.chat_history_table)
    for seq in (1, 2):
        repo.append(_turn("s1", seq))
    for seq in (3, 4, 5):
        table.put_item(
            Item={"session_id": "s1", "sequence_number": seq, "record_type": "trace_event", "x": 1}
        )
    page = repo.load_page("s1", limit=2)
    assert [r.sequence_number for r in page.items] == [1, 2]
    assert page.next_key is None


def test_delete_session_still_not_implemented(session):
    with pytest.raises(NotImplementedError):
        ChatHistoryRepository(session).delete_session("s1")
