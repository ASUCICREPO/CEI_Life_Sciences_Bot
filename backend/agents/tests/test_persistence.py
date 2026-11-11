from __future__ import annotations

from datetime import UTC, datetime

import boto3
import pytest
from moto import mock_aws

from shared.config import config
from shared.persistence.chat_history import (
    MAX_ITEM_BYTES,
    ChatHistoryRecord,
    ChatHistoryRepository,
    ContentBlock,
    HistoryItemTooLargeError,
    SequenceConflictError,
    fit_item,
)
from shared.persistence.chat_metadata import (
    ChatMetadataRecord,
    ChatMetadataRepository,
    ChatMode,
    ChatStatus,
    SessionNotFoundError,
    SortKey,
    TitleSource,
    to_iso,
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
                {"AttributeName": "created_at", "AttributeType": "S"},
                {"AttributeName": "updated_at", "AttributeType": "S"},
                {"AttributeName": "last_activity_at", "AttributeType": "S"},
                {"AttributeName": "title_search", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": name,
                    "KeySchema": [
                        {"AttributeName": "user_id", "KeyType": "HASH"},
                        {"AttributeName": sk, "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
                for name, sk in (
                    ("by_created", "created_at"),
                    ("by_updated", "updated_at"),
                    ("by_activity", "last_activity_at"),
                    ("by_title", "title_search"),
                )
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


def _turn(session_id: str, seq: int, user_id: str | None = "u1") -> ChatHistoryRecord:
    return ChatHistoryRecord(
        session_id=session_id,
        message_id=f"m{seq}",
        role="human",
        user_id=user_id,
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


def test_initializes_legacy_settings_without_overwriting_concurrent_mode(session):
    repo = ChatMetadataRepository(session)
    repo.create(_meta(mode=None, model_id=None))
    repo.update("u1", "s1", mode=ChatMode.SCIENCE)
    winner = repo.initialize_turn_settings("u1", "s1", mode="business", model_id="model-a")
    assert winner.mode == ChatMode.SCIENCE and winner.model_id is None
    updated = repo.initialize_turn_settings("u1", "s1", mode=None, model_id="model-a")
    assert updated.mode == ChatMode.SCIENCE and updated.model_id == "model-a"
    with pytest.raises(SessionNotFoundError):
        repo.initialize_turn_settings("other", "s1", mode="business", model_id="model-a")


def test_new_record_round_trips_document_and_analysis_type(session):
    repo = ChatMetadataRepository(session)
    repo.create(_meta(agent_type="drafting", document_type="protocol", analysis_type="competitor"))
    got = repo.get("u1", "s1")
    assert got is not None
    assert (got.document_type, got.analysis_type) == ("protocol", "competitor")
    # legacy rows without the new attributes still load
    session.resource("dynamodb").Table(config.chat_metadata_table).put_item(
        Item={"user_id": "u1", "session_id": "legacy", "title": "t",
              "created_at": to_iso(NOW), "updated_at": to_iso(NOW)}
    )
    legacy = repo.get("u1", "legacy")
    assert legacy is not None and legacy.document_type is None and legacy.analysis_type is None


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


def test_increment_message_count_returns_new_count_as_sequence(session):
    repo = ChatMetadataRepository(session)
    repo.create(_meta())
    assert repo.increment_message_count("u1", "s1") == 1
    assert repo.increment_message_count("u1", "s1") == 2
    got = repo.get("u1", "s1")
    assert got.message_count == 2 and got.updated_at > NOW


def test_increment_message_count_missing_session_raises(session):
    with pytest.raises(SessionNotFoundError):
        ChatMetadataRepository(session).increment_message_count("u1", "nope")


def test_get_or_create_is_idempotent_and_keeps_existing_row(session):
    repo = ChatMetadataRepository(session)
    first = repo.get_or_create(_meta())
    assert first.title == "t"
    repo.increment_message_count("u1", "s1")
    again = repo.get_or_create(_meta().model_copy(update={"title": "other"}))
    assert (again.title, again.message_count) == ("t", 1)  # existing row returned, not overwritten


def test_title_source_defaults_and_rename_marks_user(session):
    repo = ChatMetadataRepository(session)
    repo.create(_meta())
    assert repo.get("u1", "s1").title_source == TitleSource.PROMPT
    renamed = repo.update("u1", "s1", title="mine")
    assert renamed.title_source == TitleSource.USER


def test_set_generated_title_only_replaces_prompt_titles(session):
    repo = ChatMetadataRepository(session)
    repo.create(_meta(session_id="a"))
    repo.create(_meta(session_id="b"))
    assert repo.set_generated_title("u1", "a", "Generated") is True
    got = repo.get("u1", "a")
    assert (got.title, got.title_source) == ("Generated", TitleSource.GENERATED)
    repo.update_title("u1", "b", "User rename")
    assert repo.set_generated_title("u1", "b", "Generated") is False
    assert repo.get("u1", "b").title == "User rename"
    assert repo.set_generated_title("u1", "missing", "x") is False


def test_set_generated_title_on_legacy_row_without_title_source(session):
    table = session.resource("dynamodb").Table(config.chat_metadata_table)
    table.put_item(Item={"user_id": "u1", "session_id": "old", "title": "legacy",
                         "created_at": to_iso(NOW), "updated_at": to_iso(NOW)})
    repo = ChatMetadataRepository(session)
    assert repo.get("u1", "old").title_source == TitleSource.PROMPT
    assert repo.set_generated_title("u1", "old", "New") is True


def test_timestamps_are_fixed_width_utc_z(session):
    from datetime import timedelta, timezone

    repo = ChatMetadataRepository(session)
    plus2 = datetime(2026, 10, 1, 2, 0, tzinfo=timezone(timedelta(hours=2)))  # == NOW in UTC
    repo.create(_meta(session_id="tz").model_copy(update={"created_at": plus2}))
    item = session.resource("dynamodb").Table(config.chat_metadata_table).get_item(
        Key={"user_id": "u1", "session_id": "tz"})["Item"]
    assert item["created_at"] == "2026-10-01T00:00:00.000000Z"
    assert item["updated_at"] == "2026-10-01T00:00:00.000000Z"


def test_archive_and_status_filter(session):
    repo = ChatMetadataRepository(session)
    repo.create(_meta(session_id="a"))
    repo.create(_meta(session_id="b"))
    repo.archive("u1", "a")
    assert repo.get("u1", "a").status == ChatStatus.ARCHIVED
    active = repo.list_by_user("u1", status=ChatStatus.ACTIVE)
    assert [r.session_id for r in active.items] == ["b"]


def test_list_by_user_pages_newest_first_and_isolates_users(session):
    from datetime import timedelta

    repo = ChatMetadataRepository(session)
    for i in range(5):
        t = NOW + timedelta(minutes=i)
        repo.create(_meta(session_id=f"s{i}").model_copy(update={"created_at": t, "updated_at": t}))
    repo.create(_meta(user_id="u2", session_id="other"))
    first = repo.list_by_user("u1", limit=2)  # default order: last activity
    assert [r.session_id for r in first.items] == ["s4", "s3"]
    assert first.next_key == {
        "user_id": "u1", "session_id": "s3", "last_activity_at": to_iso(NOW + timedelta(minutes=3))
    }
    second = repo.list_by_user("u1", limit=10, start_key=first.next_key)
    assert [r.session_id for r in second.items] == ["s2", "s1", "s0"]
    assert second.next_key is None


def test_list_by_user_sorts_by_created_or_updated(session):
    from datetime import timedelta

    repo = ChatMetadataRepository(session)
    # 'old' was created first but updated last; 'new' was created last
    repo.create(_meta(session_id="old").model_copy(
        update={"created_at": NOW, "updated_at": NOW + timedelta(hours=2)}))
    repo.create(_meta(session_id="new").model_copy(
        update={"created_at": NOW + timedelta(hours=1), "updated_at": NOW + timedelta(hours=1)}))
    by_updated = repo.list_by_user("u1", sort=SortKey.UPDATED)
    assert [r.session_id for r in by_updated.items] == ["old", "new"]
    by_created = repo.list_by_user("u1", sort=SortKey.CREATED)
    assert [r.session_id for r in by_created.items] == ["new", "old"]


def test_user_message_moves_old_chat_to_top_of_activity_order(session):
    from datetime import timedelta

    repo = ChatMetadataRepository(session)
    repo.create(_meta(session_id="old"))
    later = NOW + timedelta(hours=1)
    repo.create(_meta(session_id="new").model_copy(update={"created_at": later, "updated_at": later}))
    assert [r.session_id for r in repo.list_by_user("u1").items] == ["new", "old"]

    repo.increment_message_count("u1", "old", user_activity=True)  # the user writes in the old chat
    assert [r.session_id for r in repo.list_by_user("u1").items] == ["old", "new"]

    repo.increment_message_count("u1", "new")  # an AI answer is not user activity
    repo.update("u1", "new", title="Renamed")  # neither is a rename
    assert [r.session_id for r in repo.list_by_user("u1").items] == ["old", "new"]


def test_title_search_is_case_insensitive_substring_and_owner_scoped(session):
    repo = ChatMetadataRepository(session)
    repo.create(_meta(session_id="a").model_copy(update={"title": "KRAS G12C resistance"}))
    repo.create(_meta(session_id="b").model_copy(update={"title": "Grant draft"}))
    repo.create(_meta(user_id="u2", session_id="c").model_copy(update={"title": "kras for u2"}))
    assert [r.session_id for r in repo.list_by_user("u1", query="  Kras ").items] == ["a"]
    assert [r.session_id for r in repo.list_by_user("u1", query="RESIST").items] == ["a"]
    assert repo.list_by_user("u1", query="nothing").items == []

    repo.update("u1", "b", title="Pitch deck")  # renames keep the search attribute in step
    assert [r.session_id for r in repo.list_by_user("u1", query="pitch").items] == ["b"]
    assert repo.list_by_user("u1", query="grant").items == []


def test_title_search_pages_without_skipping_matches(session):
    repo = ChatMetadataRepository(session)
    for i in range(7):
        repo.create(_meta(session_id=f"m{i}").model_copy(update={"title": f"match {i}"}))
        repo.create(_meta(session_id=f"x{i}").model_copy(update={"title": f"other {i}"}))
    seen: list[str] = []
    page = repo.list_by_user("u1", query="match", limit=3)
    while True:
        assert len(page.items) <= 3
        seen.extend(r.session_id for r in page.items)
        if page.next_key is None:
            break
        page = repo.list_by_user("u1", query="match", limit=3, start_key=page.next_key)
    assert seen == [f"m{i}" for i in range(7)]


def test_load_page_cursor_and_chronological_order(session):
    repo = ChatHistoryRepository(session)
    for seq in range(1, 6):
        repo.append(_turn("s1", seq))
    page1 = repo.load_page("u1", "s1", limit=2)
    assert [r.sequence_number for r in page1.items] == [4, 5]
    assert page1.next_key == {"session_id": "s1", "sequence_number": 4}
    page2 = repo.load_page("u1", "s1", limit=2, start_key=page1.next_key)
    assert [r.sequence_number for r in page2.items] == [2, 3]
    page3 = repo.load_page("u1", "s1", limit=2, start_key=page2.next_key)
    assert [r.sequence_number for r in page3.items] == [1]
    assert page3.next_key is None


def test_load_page_empty_session(session):
    page = ChatHistoryRepository(session).load_page("u1", "none")
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
    page = repo.load_page("u1", "s1", limit=2)
    assert [r.sequence_number for r in page.items] == [1, 2]
    assert page.next_key is None


def test_append_never_overwrites_a_sequence_number(session):
    repo = ChatHistoryRepository(session)
    repo.append(_turn("s1", 1))
    with pytest.raises(SequenceConflictError):
        repo.append(_turn("s1", 1))


def test_append_requires_owner(session):
    with pytest.raises(ValueError):
        ChatHistoryRepository(session).append(_turn("s1", 1, user_id=None))


def test_append_accepts_float_payloads(session):
    repo = ChatHistoryRepository(session)
    rec = _turn("s1", 1).model_copy(update={"citations": [{"score": 0.87}]})
    repo.append(rec)
    assert repo.load_page("u1", "s1").items[0].citations == [{"score": 0.87}]


def test_load_page_drops_items_of_another_owner_but_keeps_legacy(session):
    repo = ChatHistoryRepository(session)
    table = session.resource("dynamodb").Table(config.chat_history_table)
    repo.append(_turn("s1", 1))
    repo.append(_turn("s1", 2, user_id="intruder"))
    legacy = _turn("s1", 3, user_id=None).model_dump(mode="json")
    legacy.pop("user_id")
    table.put_item(Item=legacy)
    assert [r.sequence_number for r in repo.load_page("u1", "s1").items] == [1, 3]
    assert [r.sequence_number for r in repo.load_page("intruder", "s1").items] == [2, 3]


def test_load_recent_returns_last_n_oldest_first(session):
    repo = ChatHistoryRepository(session)
    for seq in range(1, 8):
        repo.append(_turn("s1", seq))
    assert [r.sequence_number for r in repo.load_recent("u1", "s1", 3)] == [5, 6, 7]
    assert repo.load_recent("u1", "s1", 0) == []


def test_owner_of(session):
    repo = ChatHistoryRepository(session)
    assert repo.owner_of("s1") is None
    repo.append(_turn("s1", 1))
    assert repo.owner_of("s1") == "u1"


def test_fit_item_trims_tool_output_then_thinking_then_text():
    big = "x" * (MAX_ITEM_BYTES + 10)
    base = _turn("s1", 1).model_dump(mode="json")
    small_enough = {**base, "tool_output": {"blob": big}}
    fitted = fit_item(small_enough)
    assert fitted["tool_output"] is None and fitted["truncated"] is True

    with_thinking = {**base, "content_blocks": [
        {"block_type": "text", "body": "keep me"}, {"block_type": "thinking", "body": big}]}
    fitted = fit_item(with_thinking)
    assert fitted["content_blocks"] == [{"block_type": "text", "body": "keep me"}]

    long_text = {**base, "content_blocks": [{"block_type": "text", "body": "é" * MAX_ITEM_BYTES}]}
    fitted = fit_item(long_text)
    body = fitted["content_blocks"][0]["body"]
    assert body.endswith("[truncated]") and fitted["truncated"] is True
    import json
    assert len(json.dumps(fitted, ensure_ascii=False).encode()) <= MAX_ITEM_BYTES


def test_fit_item_leaves_small_items_untouched_and_raises_when_untrimmable():
    base = _turn("s1", 1).model_dump(mode="json")
    assert fit_item(base) is base
    huge_ui = {**base, "content_blocks": [{"block_type": "a2ui", "body": {"x": "y" * (MAX_ITEM_BYTES + 1)}}]}
    with pytest.raises(HistoryItemTooLargeError):
        fit_item(huge_ui)


def test_append_persists_trimmed_oversized_item(session):
    repo = ChatHistoryRepository(session)
    rec = _turn("s1", 1).model_copy(update={"content_blocks": [
        ContentBlock(block_type="text", body="z" * (MAX_ITEM_BYTES + 1000))]})
    repo.append(rec)
    got = repo.load_page("u1", "s1").items[0]
    assert got.truncated is True and got.content_blocks[0].body.endswith("[truncated]")


def _recorder(session):
    from shared.persistence.conversation import ConversationRecorder

    return ConversationRecorder(ChatMetadataRepository(session), ChatHistoryRepository(session))


def test_recorder_human_turn_advances_activity_and_ai_turn_does_not(session):
    import asyncio
    from datetime import timedelta

    past = datetime(2020, 1, 1, tzinfo=UTC)
    repo = ChatMetadataRepository(session)
    repo.create(_meta(session_id="old").model_copy(
        update={"created_at": past, "updated_at": past, "last_activity_at": past}))
    later = past + timedelta(hours=1)
    repo.create(_meta(session_id="new").model_copy(
        update={"created_at": later, "updated_at": later, "last_activity_at": later}))
    assert [r.session_id for r in repo.list_by_user("u1", sort=SortKey.ACTIVITY).items] == ["new", "old"]
    rec = _recorder(session)

    asyncio.run(rec.record_human_action(user_id="u1", session_id="old", text="approve"))  # HUMAN turn
    old = repo.get("u1", "old")
    assert old.last_activity_at > later
    assert [r.session_id for r in repo.list_by_user("u1", sort=SortKey.ACTIVITY).items] == ["old", "new"]

    seq = asyncio.run(rec.finish_turn(user_id="u1", session_id="new", text="answer"))  # AI turn
    assert seq == 1
    new = repo.get("u1", "new")
    assert new.last_activity_at == later
    assert new.updated_at > later  # the AI write did land
    assert [r.session_id for r in repo.list_by_user("u1", sort=SortKey.ACTIVITY).items] == ["old", "new"]


def test_recorder_foreign_owner_refused_before_any_write(session):
    import asyncio

    rec = _recorder(session)
    asyncio.run(rec.start_turn(user_id="u1", session_id="s1", prompt="mine"))
    history = ChatHistoryRepository(session)
    before = history.load_recent("u1", "s1", 50)
    assert len(before) == 1

    with pytest.raises(SessionNotFoundError):
        asyncio.run(rec.start_turn(user_id="u2", session_id="s1", prompt="intruder"))
    with pytest.raises(SessionNotFoundError):
        asyncio.run(rec.record_human_action(user_id="u2", session_id="s1", text="intruder"))
    with pytest.raises(SessionNotFoundError):
        asyncio.run(rec.assert_owner(user_id="u2", session_id="s1"))

    items = session.resource("dynamodb").Table(config.chat_history_table).scan()["Items"]
    assert len(items) == 1 and items[0]["user_id"] == "u1"
    assert ChatMetadataRepository(session).get("u2", "s1") is None  # no metadata row stamped for u2
    assert ChatMetadataRepository(session).get("u1", "s1").message_count == 1


def test_delete_session_removes_all_partition_items_only(session):
    table = session.resource("dynamodb").Table(config.chat_history_table)
    for sid, seq in [("s1", -1002), ("s1", -1), ("s1", 0), ("s1", 1), ("s2", 0)]:
        table.put_item(Item={"session_id": sid, "sequence_number": seq, "user_id": "u1"})

    repository = ChatHistoryRepository(session)
    repository.delete_session("s1")
    repository.delete_session("s1")  # retry is safe
    assert table.scan()["Items"] == [{"session_id": "s2", "sequence_number": 0, "user_id": "u1"}]


def test_delete_session_requires_partition(session):
    with pytest.raises(ValueError):
        ChatHistoryRepository(session).delete_session("")


def test_chat_metadata_delete_removes_only_the_one_row(session):
    repo = ChatMetadataRepository(session)
    repo.create(_meta(user_id="u1", session_id="s1"))
    repo.create(_meta(user_id="u1", session_id="s2"))
    repo.delete("u1", "s1")
    assert repo.get("u1", "s1") is None
    assert repo.get("u1", "s2") is not None


def test_chat_metadata_delete_is_idempotent(session):
    repo = ChatMetadataRepository(session)
    repo.delete("u1", "never-existed")  # no error
