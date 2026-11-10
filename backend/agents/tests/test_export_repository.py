from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from shared.persistence.exports import ExportRepository

S1 = "6f1c2d3e-0000-4000-8000-000000000001"
S2 = "6f1c2d3e-0000-4000-8000-000000000002"


@pytest.fixture
def repository(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(TableName="exports", BillingMode="PAY_PER_REQUEST",
                         KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"},
                                    {"AttributeName": "export_id", "KeyType": "RANGE"}],
                         AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"},
                                               {"AttributeName": "export_id", "AttributeType": "S"}])
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="cei-export-test")
        yield ExportRepository("exports", "cei-export-test", dynamodb=ddb, s3=s3), s3


def save(repo, *, owner="alice", session=S1, reviewed=True):
    return repo.save(user_id=owner, session_id=session, turn_id="turn-1", filename="report.pdf",
                     format="pdf", data=b"report data", reviewed=reviewed)


def test_review_required_and_s3_intelligent_tiering(repository):
    repo, s3 = repository
    with pytest.raises(ValueError, match="review"):
        save(repo, reviewed=False)
    assert s3.list_objects_v2(Bucket="cei-export-test").get("KeyCount") == 0
    item = save(repo)
    assert s3.head_object(Bucket="cei-export-test", Key=item["s3_key"])["StorageClass"] == "INTELLIGENT_TIERING"
    assert repo.get("bob", item["export_id"]) is None
    assert repo.get("alice", item["export_id"])["turn_id"] == "turn-1"


def test_strongly_consistent_session_pagination_and_deletion(repository):
    repo, s3 = repository
    expected = [save(repo) for _ in range(3)]
    save(repo, session=S2)
    save(repo, owner="bob")
    page = repo.list_for_session("alice", S1, limit=2)
    assert len(page.items) == 2 and page.next_key
    page2 = repo.list_for_session("alice", S1, limit=2, start_key=page.next_key)
    assert {x["export_id"] for x in page.items + page2.items} == {x["export_id"] for x in expected}
    item = expected[0]
    repo.delete("bob", item["export_id"])
    assert repo.get("alice", item["export_id"])
    repo.delete("alice", item["export_id"])
    assert repo.get("alice", item["export_id"]) is None
    with pytest.raises(s3.exceptions.ClientError):
        s3.head_object(Bucket="cei-export-test", Key=item["s3_key"])


def test_metadata_failure_removes_uploaded_object(repository, monkeypatch):
    repo, s3 = repository
    def fail(**kwargs):
        raise RuntimeError("metadata unavailable")
    monkeypatch.setattr(repo._table, "put_item", fail)
    with pytest.raises(RuntimeError):
        save(repo)
    assert s3.list_objects_v2(Bucket="cei-export-test").get("KeyCount") == 0


def test_delete_session_paginates_without_s3_listing(repository, monkeypatch):
    repo, s3 = repository
    for _ in range(102):
        save(repo)
    other_chat = save(repo, session=S2)
    other_user = save(repo, owner="bob")
    def fail_listing(**kwargs):
        raise AssertionError("deletion must not list S3")
    monkeypatch.setattr(s3, "list_objects_v2", fail_listing)
    repo.delete_session("alice", S1)
    assert repo.list_for_session("alice", S1).items == []
    assert repo.get("alice", other_chat["export_id"])
    assert repo.get("bob", other_user["export_id"])
    repo.delete_session("alice", S1)  # Retry after completion is safe.
