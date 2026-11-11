from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

MODULE_PATH = Path(__file__).resolve().parents[1] / "handler.py"
LAMBDAS = Path(__file__).resolve().parents[2]
# Same as the deployed layer: `http_errors` from lambdas/shared, `shared.persistence` from backend/agents.
for _path in (str(LAMBDAS / "shared"), str(LAMBDAS.parent / "agents")):
    if _path not in sys.path:
        sys.path.insert(0, _path)


def _load_handler():
    spec = importlib.util.spec_from_file_location("account_delete_handler", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


USER_POOL_ID_ENV = "us-east-1_test00000"
SCHEDULE_GROUP = "cei-account-delete"
WORKER_ARN = "arn:aws:lambda:us-east-1:123456789012:function:worker"
SCHEDULER_ROLE_ARN = "arn:aws:iam::123456789012:role/scheduler"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("CEI_AWS_REGION", "us-east-1")
    monkeypatch.setenv("CEI_USER_METADATA_TABLE", "UserMetadataTable")
    monkeypatch.setenv("USER_POOL_ID", USER_POOL_ID_ENV)
    monkeypatch.setenv("SCHEDULE_GROUP_NAME", SCHEDULE_GROUP)
    monkeypatch.setenv("DELETE_WORKER_ARN", WORKER_ARN)
    monkeypatch.setenv("SCHEDULER_ROLE_ARN", SCHEDULER_ROLE_ARN)
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
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
        cognito = boto3.client("cognito-idp", region_name="us-east-1")
        pool = cognito.create_user_pool(PoolName="cei-test-pool")
        pool_id = pool["UserPool"]["Id"]
        monkeypatch.setenv("USER_POOL_ID", pool_id)
        user = cognito.admin_create_user(UserPoolId=pool_id, Username="someone@example.com")
        username = user["User"]["Username"]
        scheduler = boto3.client("scheduler", region_name="us-east-1")
        scheduler.create_schedule_group(Name=SCHEDULE_GROUP)
        yield {"pool_id": pool_id, "username": username}


def _event(sub: str = "user-sub-1", username: str = "someone@example.com") -> dict:
    return {
        "requestContext": {
            "requestId": "req-1",
            "authorizer": {"claims": {"sub": sub, "cognito:username": username}},
        },
        "httpMethod": "POST",
        "resource": "/account/delete",
    }


def test_returns_202_and_disables_and_signs_out_and_schedules(env):
    handler = _load_handler()
    event = _event(username=env["username"])
    response = handler.handler(event, None)
    assert response["statusCode"] == 202
    assert '"status": "deleting"' in response["body"] or '"status":"deleting"' in response["body"]

    cognito = boto3.client("cognito-idp", region_name="us-east-1")
    user = cognito.admin_get_user(UserPoolId=env["pool_id"], Username=env["username"])
    assert user["Enabled"] is False

    scheduler = boto3.client("scheduler", region_name="us-east-1")
    schedules = scheduler.list_schedules(GroupName=SCHEDULE_GROUP)["Schedules"]
    assert len(schedules) == 1


def test_repeat_call_is_idempotent_and_returns_202_again(env):
    handler = _load_handler()
    event = _event(username=env["username"])
    first = handler.handler(event, None)
    second = handler.handler(event, None)
    assert first["statusCode"] == 202
    assert second["statusCode"] == 202

    scheduler = boto3.client("scheduler", region_name="us-east-1")
    schedules = scheduler.list_schedules(GroupName=SCHEDULE_GROUP)["Schedules"]
    # The repeat call must not create a second schedule for the same user (B109 idempotency).
    assert len(schedules) == 1

    gate = handler._gate().get("user-sub-1")
    assert gate is not None


def test_gate_is_written_before_any_cognito_call(env, monkeypatch):
    handler = _load_handler()

    def _boom(*args, **kwargs):
        raise RuntimeError("cognito unavailable")

    monkeypatch.setattr(handler, "disable_and_sign_out", _boom)
    event = _event(username=env["username"])
    response = handler.handler(event, None)
    assert response["statusCode"] == 500

    gate = handler._gate().get("user-sub-1")
    assert gate is not None
    assert gate.requested_at is not None


def test_user_id_comes_only_from_verified_claims(env):
    handler = _load_handler()
    event = {"requestContext": {"requestId": "req-2", "authorizer": {"claims": {}}},
             "httpMethod": "POST", "resource": "/account/delete"}
    response = handler.handler(event, None)
    assert response["statusCode"] == 401


def test_schedule_time_is_now_plus_nine_hours(env, monkeypatch):
    handler = _load_handler()
    fixed_now = datetime(2026, 11, 11, 0, 0, 0, tzinfo=UTC)

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now

    monkeypatch.setattr(handler, "datetime", _FixedDateTime)
    event = _event(username=env["username"])
    handler.handler(event, None)

    gate = handler._gate().get("user-sub-1")
    assert gate.requested_at == fixed_now
    assert gate.sweep_at == fixed_now + timedelta(hours=9)
