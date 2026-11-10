from __future__ import annotations

import boto3
import pytest
from moto import mock_aws
from unittest.mock import Mock

from drafting.export_service import generate_reviewed_export
from shared.persistence.exports import ExportRepository
from shared.persistence.writer_fence import WriteFenced, WriterFence

CHAT = "6f1c2d3e-0000-4000-8000-000000000001"
OTHER = "6f1c2d3e-0000-4000-8000-000000000002"
JOB = "a" * 32


@pytest.fixture
def fence(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        for name, sort in (("fences", "scope"), ("metadata", "session_id")):
            client.create_table(TableName=name, BillingMode="PAY_PER_REQUEST",
                                KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"},
                                           {"AttributeName": sort, "KeyType": "RANGE"}],
                                AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"},
                                                      {"AttributeName": sort, "AttributeType": "S"}])
        for user, chat in (("alice", CHAT), ("alice", OTHER), ("bob", CHAT)):
            client.put_item(TableName="metadata", Item={"user_id": {"S": user}, "session_id": {"S": chat}})
        yield WriterFence(client, "fences", "metadata")


def test_chat_fence_blocks_new_writers_and_waits_for_existing(fence):
    writer = fence.acquire("alice", CHAT, writer_ref="run-1")
    other_chat = fence.acquire("alice", OTHER, writer_ref="run-2")
    other_user = fence.acquire("bob", CHAT, writer_ref="run-3")
    fence.begin_deletion("alice", JOB, CHAT)
    fence.begin_deletion("alice", JOB, CHAT)  # Retry is idempotent for the same job.
    with pytest.raises(WriteFenced):
        fence.acquire("alice", CHAT, writer_ref="run-1")
    with pytest.raises(WriteFenced):
        fence.begin_deletion("alice", "b" * 32, CHAT)
    assert not fence.quiescent("alice", JOB, CHAT)
    with pytest.raises(WriteFenced):
        fence.finish_chat_deletion("alice", CHAT, JOB)
    fence.release(writer)
    fence.release(writer)  # Recovery/retry is idempotent.
    assert fence.quiescent("alice", JOB, CHAT)
    # Writers in another chat and another account do not block chat deletion.
    fence.release(other_chat)
    fence.release(other_user)


def test_account_fence_waits_for_all_user_chats(fence):
    first = fence.acquire("alice", CHAT, writer_ref="run-1")
    second = fence.acquire("alice", OTHER, writer_ref="run-2")
    fence.begin_deletion("alice", JOB)
    assert not fence.quiescent("alice", JOB)
    with pytest.raises(WriteFenced):
        fence.acquire("alice", OTHER, writer_ref="run-2")
    with pytest.raises(WriteFenced):
        fence.begin_deletion("alice", "b" * 32, CHAT)
    fence.release(first)
    assert not fence.quiescent("alice", JOB)
    fence.release(second)
    assert fence.quiescent("alice", JOB)
    fence.release(fence.acquire("bob", CHAT, writer_ref="run-3"))


def test_account_deletion_waits_for_chat_deletion_cascades(fence):
    fence.begin_deletion("alice", JOB, CHAT)
    fence.begin_deletion("alice", JOB, CHAT)  # Must not increment the counter again.
    fence.begin_deletion("alice", "b" * 32, OTHER)
    fence.begin_deletion("alice", "c" * 32)
    assert not fence.quiescent("alice", "c" * 32)
    with pytest.raises(WriteFenced):
        fence.acquire("alice", CHAT, writer_ref="run-1")
    fence.finish_chat_deletion("alice", CHAT, JOB)
    fence.finish_chat_deletion("alice", CHAT, JOB)  # Must not decrement twice.
    assert not fence.quiescent("alice", "c" * 32)
    with pytest.raises(WriteFenced):
        fence.finish_chat_deletion("alice", OTHER, JOB)
    fence.finish_chat_deletion("alice", OTHER, "b" * 32)
    assert fence.quiescent("alice", "c" * 32)
    with pytest.raises(WriteFenced):
        fence.begin_deletion("alice", JOB, CHAT)
    with pytest.raises(WriteFenced):
        fence.acquire("alice", CHAT, writer_ref="run-1")


def test_fence_and_session_must_exist(fence):
    with pytest.raises(WriteFenced):
        fence.acquire("mallory", CHAT, writer_ref="run-1")
    with pytest.raises(WriteFenced):
        fence.quiescent("alice", JOB, CHAT)
    fence.begin_deletion("alice", JOB, CHAT)
    with pytest.raises(WriteFenced):
        fence.quiescent("alice", "b" * 32, CHAT)


def test_uncertain_writer_failure_remains_tracked(fence):
    token = None
    with pytest.raises(TimeoutError):
        with fence.writing("alice", CHAT, writer_ref="run-1") as token:
            raise TimeoutError("remote upload result is unknown")
    fence.begin_deletion("alice", JOB, CHAT)
    assert not fence.quiescent("alice", JOB, CHAT)
    # A real recovery adapter must first prove the writer and in-flight writes stopped.
    fence.release(token)
    assert fence.quiescent("alice", JOB, CHAT)


def test_uncertain_acquire_leaves_recoverable_writer_reference(fence, monkeypatch):
    original = fence._client.transact_write_items
    def ambiguous(**kwargs):
        original(**kwargs)
        raise TimeoutError("transaction response lost")
    monkeypatch.setattr(fence._client, "transact_write_items", ambiguous)
    with pytest.raises(TimeoutError):
        fence.acquire("alice", CHAT, writer_ref="durable-run-1")
    monkeypatch.setattr(fence._client, "transact_write_items", original)
    fence.begin_deletion("alice", JOB, CHAT)
    assert not fence.quiescent("alice", JOB, CHAT)
    records = fence._client.query(
        TableName="fences", ConsistentRead=True,
        KeyConditionExpression="user_id = :u AND begins_with(#scope, :prefix)",
        ExpressionAttributeNames={"#scope": "scope"},
        ExpressionAttributeValues={":u": {"S": "alice"}, ":prefix": {"S": f"writer#{CHAT}#"}},
    )["Items"]
    assert len(records) == 1 and records[0]["writer_ref"]["S"] == "durable-run-1"


def test_successful_writer_releases_on_exit(fence):
    with fence.writing("alice", CHAT, writer_ref="run-1"):
        pass
    fence.begin_deletion("alice", JOB, CHAT)
    assert fence.quiescent("alice", JOB, CHAT)


def test_report_writer_blocks_deletion_until_storage_finishes(fence):
    repo = Mock()
    def store(**kwargs):
        fence.begin_deletion("alice", JOB, CHAT)
        assert not fence.quiescent("alice", JOB, CHAT)
        return {"export_id": "report"}
    repo.save.side_effect = store
    generate_reviewed_export(repository=repo, writer_fence=fence, writer_ref="run-1", user_id="alice", session_id=CHAT,
                             turn_id="turn", reviewed=True, format="md", title="Report",
                             sections=[{"heading": "Result", "content": "Text"}])
    assert fence.quiescent("alice", JOB, CHAT)


def test_report_storage_timeout_keeps_deletion_blocked(fence):
    repo = Mock()
    repo.save.side_effect = TimeoutError("uncertain upload")
    with pytest.raises(TimeoutError):
        generate_reviewed_export(repository=repo, writer_fence=fence, writer_ref="run-1", user_id="alice", session_id=CHAT,
                                 turn_id="turn", reviewed=True, format="md", title="Report",
                                 sections=[{"heading": "Result", "content": "Text"}])
    fence.begin_deletion("alice", JOB, CHAT)
    assert not fence.quiescent("alice", JOB, CHAT)


def test_report_object_is_deleted_only_after_writer_quiesces(fence):
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    dynamodb.create_table(TableName="exports", BillingMode="PAY_PER_REQUEST",
                          KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"},
                                     {"AttributeName": "export_id", "KeyType": "RANGE"}],
                          AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"},
                                                {"AttributeName": "export_id", "AttributeType": "S"}])
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="cei-fence-test")
    repo = ExportRepository("exports", "cei-fence-test", dynamodb=dynamodb, s3=s3)
    save = repo.save
    def save_then_begin_delete(**kwargs):
        result = save(**kwargs)
        fence.begin_deletion("alice", JOB, CHAT)
        assert not fence.quiescent("alice", JOB, CHAT)
        return result
    repo.save = save_then_begin_delete
    report = generate_reviewed_export(repository=repo, writer_fence=fence, writer_ref="run-1", user_id="alice", session_id=CHAT,
                                      turn_id="turn", reviewed=True, format="md", title="Report",
                                      sections=[{"content": "Text"}])
    assert fence.quiescent("alice", JOB, CHAT)
    repo.delete_session("alice", CHAT)
    assert repo.get("alice", report["export_id"]) is None
    assert s3.list_objects_v2(Bucket="cei-fence-test")["KeyCount"] == 0
