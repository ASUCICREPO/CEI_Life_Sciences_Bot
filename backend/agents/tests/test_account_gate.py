from __future__ import annotations

from datetime import UTC, datetime, timedelta

import boto3
import pytest
from moto import mock_aws

from shared.persistence.account_gate import AccountGateRepository, schedule_name

NOW = datetime(2026, 11, 11, 0, 0, 0, tzinfo=UTC)


@pytest.fixture
def session(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    with mock_aws():
        sess = boto3.Session(region_name="us-east-1")
        ddb = sess.resource("dynamodb")
        ddb.create_table(
            TableName="UserMetadataTable",
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}, {"AttributeName": "record", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"}, {"AttributeName": "record", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield sess


def test_write_gate_then_is_deleting_and_get(session):
    repo = AccountGateRepository(session)
    assert repo.is_deleting("u1") is False
    gate = repo.write_gate("u1", requested_at=NOW, sweep_at=NOW + timedelta(hours=9), cognito_username="u1name")
    assert gate.requested_at == NOW
    assert gate.sweep_at == NOW + timedelta(hours=9)
    assert gate.cognito_username == "u1name"
    assert repo.is_deleting("u1") is True
    assert repo.get("u1") == gate


def test_write_gate_is_idempotent_and_keeps_the_original_sweep_at(session):
    repo = AccountGateRepository(session)
    first = repo.write_gate("u1", requested_at=NOW, sweep_at=NOW + timedelta(hours=9), cognito_username="u1name")
    later = NOW + timedelta(hours=1)
    second = repo.write_gate("u1", requested_at=later, sweep_at=later + timedelta(hours=9), cognito_username="u1name")
    assert second == first  # the retry never moves sweep_at later
    assert repo.get("u1").sweep_at == NOW + timedelta(hours=9)


def test_write_gate_does_not_affect_other_users(session):
    repo = AccountGateRepository(session)
    repo.write_gate("u1", requested_at=NOW, sweep_at=NOW + timedelta(hours=9))
    assert repo.is_deleting("u2") is False


def test_delete_gate_is_idempotent(session):
    repo = AccountGateRepository(session)
    repo.write_gate("u1", requested_at=NOW, sweep_at=NOW + timedelta(hours=9))
    repo.delete_gate("u1")
    assert repo.is_deleting("u1") is False
    repo.delete_gate("u1")  # no error


def test_schedule_name_is_deterministic_and_never_embeds_the_user_id():
    name1 = schedule_name("user-sub-1")
    name2 = schedule_name("user-sub-1")
    name3 = schedule_name("user-sub-2")
    assert name1 == name2
    assert name1 != name3
    assert "user-sub-1" not in name1
