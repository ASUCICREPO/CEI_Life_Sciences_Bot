import boto3
import pytest
from moto import mock_aws

from shared.persistence.chat_metadata import ChatMode
from shared.persistence.user_metadata import UserMetadataRepository


@pytest.fixture
def repo(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            # shared/config.py's default (also the CDK table's physical/logical name, model-settings.ts)
            TableName="UserMetadataTable",
            KeySchema=[
                {"AttributeName": "user_id", "KeyType": "HASH"},
                {"AttributeName": "record", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "user_id", "AttributeType": "S"},
                {"AttributeName": "record", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        session = boto3.Session(region_name="us-east-1")
        yield UserMetadataRepository(session=session)


def test_get_returns_none_when_absent(repo):
    assert repo.get("alice") is None


def test_put_then_get_round_trips(repo):
    record = repo.put("alice", default_model_id="us.anthropic.claude-sonnet-v1", default_mode=ChatMode.BUSINESS)
    assert record.default_model_id == "us.anthropic.claude-sonnet-v1" and record.default_mode == ChatMode.BUSINESS
    fetched = repo.get("alice")
    assert fetched == record


def test_put_preserves_created_at_across_updates(repo):
    first = repo.put("alice", default_model_id="model-a", default_mode=ChatMode.SCIENCE)
    second = repo.put("alice", default_model_id="model-b", default_mode=None)
    assert second.created_at == first.created_at
    assert second.default_model_id == "model-b" and second.default_mode is None


def test_users_are_isolated(repo):
    repo.put("alice", default_model_id="model-a", default_mode=None)
    assert repo.get("bob") is None
