"""B109/F19: the deployed account-delete cascade (`handle`), separate from the superseded job/SQS/writer-
fence contract in test_handler.py (unchanged, still passing -- see handler.py's own module comment).
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import boto3
import pytest
from moto import mock_aws

LAMBDAS = Path(__file__).resolve().parents[1]
AGENTS = LAMBDAS.parent / "agents"

for path in (str(LAMBDAS / "shared"), str(AGENTS)):
    if path not in sys.path:
        sys.path.insert(0, path)

MODULE_PATH = Path(__file__).with_name("handler.py")
USER_POOL_NAME = "cei-test-pool"
SCHEDULE_GROUP = "cei-account-delete"
WORKER_ARN = "arn:aws:lambda:us-east-1:123456789012:function:worker"
SCHEDULER_ROLE_ARN = "arn:aws:iam::123456789012:role/scheduler"
MEMORY_ID = "memory-123"


def _load_worker():
    spec = importlib.util.spec_from_file_location("account_delete_worker", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FixedContext:
    def __init__(self, remaining_ms: int) -> None:
        self._remaining_ms = remaining_ms

    def get_remaining_time_in_millis(self) -> int:
        return self._remaining_ms


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("CEI_AWS_REGION", "us-east-1")
    monkeypatch.setenv("CEI_USER_METADATA_TABLE", "UserMetadataTable")
    monkeypatch.setenv("CEI_CHAT_METADATA_TABLE", "ChatMetadataStore")
    monkeypatch.setenv("CEI_CHAT_HISTORY_TABLE", "ChatHistoryStore")
    monkeypatch.setenv("CEI_ACTIVE_RUNS_TABLE", "ActiveRunsStore")
    monkeypatch.setenv("CEI_EXPORT_METADATA_TABLE", "ExportMetadataStore")
    monkeypatch.setenv("CEI_EXPORT_BUCKET", "exports-bucket")
    monkeypatch.setenv("CEI_JOB_TABLE", "JobIdStore")
    monkeypatch.setenv("CEI_API_KEY_SECRET_PREFIX", "cei/user-keys")
    monkeypatch.setenv("CEI_API_KEY_SOURCE_IDS", "pubmed,ncbi")
    monkeypatch.setenv("CEI_MEMORY_ID", MEMORY_ID)
    monkeypatch.setenv("CEI_SCHEDULE_GROUP_NAME", SCHEDULE_GROUP)
    monkeypatch.setenv("CEI_DELETE_WORKER_ARN", WORKER_ARN)
    monkeypatch.setenv("CEI_SCHEDULER_ROLE_ARN", SCHEDULER_ROLE_ARN)

    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="UserMetadataTable",
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}, {"AttributeName": "record", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"}, {"AttributeName": "record", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.create_table(
            TableName="ChatMetadataStore",
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}, {"AttributeName": "session_id", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "user_id", "AttributeType": "S"},
                {"AttributeName": "session_id", "AttributeType": "S"},
                {"AttributeName": "created_at", "AttributeType": "S"},
                {"AttributeName": "updated_at", "AttributeType": "S"},
                {"AttributeName": "last_activity_at", "AttributeType": "S"},
                {"AttributeName": "title_search", "AttributeType": "S"},
            ],
            # mirrors backend/lib/backend-stack.ts (ChatMetadataStore GSIs) -- list_by_user queries by_activity
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
            TableName="ChatHistoryStore",
            KeySchema=[{"AttributeName": "session_id", "KeyType": "HASH"}, {"AttributeName": "sequence_number", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "session_id", "AttributeType": "S"}, {"AttributeName": "sequence_number", "AttributeType": "N"}],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.create_table(
            TableName="ActiveRunsStore",
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}, {"AttributeName": "record", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"}, {"AttributeName": "record", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.create_table(
            TableName="ExportMetadataStore",
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}, {"AttributeName": "export_id", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"}, {"AttributeName": "export_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.create_table(
            TableName="JobIdStore",
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}, {"AttributeName": "job_id", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"}, {"AttributeName": "job_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="exports-bucket")
        secrets = boto3.client("secretsmanager", region_name="us-east-1")
        secrets.create_secret(Name="cei/user-keys/owner/pubmed", SecretString="k1")
        secrets.create_secret(Name="cei/user-keys/owner/ncbi", SecretString="k2")
        cognito = boto3.client("cognito-idp", region_name="us-east-1")
        pool_id = cognito.create_user_pool(PoolName=USER_POOL_NAME)["UserPool"]["Id"]
        username = cognito.admin_create_user(UserPoolId=pool_id, Username="owner@example.com")["User"]["Username"]
        monkeypatch.setenv("USER_POOL_ID", pool_id)
        scheduler = boto3.client("scheduler", region_name="us-east-1")
        scheduler.create_schedule_group(Name=SCHEDULE_GROUP)
        yield {"username": username, "pool_id": pool_id}


def _write_gate(worker, user_id, *, sweep_delta: timedelta, cognito_username: str | None):
    now = datetime.now(UTC)
    return worker._gate_repository().write_gate(
        user_id, requested_at=now, sweep_at=now + sweep_delta, cognito_username=cognito_username,
    )


def _seed_session(worker, user_id, session_id, *, with_export=False, with_job=False):
    from shared.persistence.chat_history import ChatHistoryRecord, ContentBlock
    from shared.persistence.chat_metadata import ChatMetadataRecord

    now = datetime.now(UTC)
    worker._chat_metadata_repository().create(
        ChatMetadataRecord(session_id=session_id, user_id=user_id, title="t", created_at=now, updated_at=now)
    )
    worker._chat_history_repository().append(ChatHistoryRecord(
        session_id=session_id, message_id="m1", role="human", user_id=user_id,
        content_blocks=[ContentBlock(block_type="text", body="hi")], timestamp=now, sequence_number=0,
    ))
    if with_export:
        worker._export_repository().save(
            user_id=user_id, session_id=session_id, turn_id="m1", filename="r.pdf",
            format="pdf", data=b"report bytes", reviewed=True,
        )
    if with_job:
        worker._job_repository().create(
            user_id=user_id, session_id=session_id, agent_type="research", runtime_session_id="rs-1",
        )


def _patch_memory(monkeypatch, worker):
    calls = {"checkpoint": [], "namespace": []}
    import shared.memory.agentcore as agentcore_module

    monkeypatch.setattr(agentcore_module, "delete_checkpoint",
                        lambda memory_id, thread_id, actor_id="": calls["checkpoint"].append((memory_id, thread_id, actor_id)))
    monkeypatch.setattr(agentcore_module, "delete_long_term_namespace",
                        lambda memory_id, user_id, **kw: calls["namespace"].append((memory_id, user_id)))
    return calls


def test_early_run_reschedules_and_deletes_nothing(env, monkeypatch):
    worker = _load_worker()
    calls = _patch_memory(monkeypatch, worker)
    gate = _write_gate(worker, "owner", sweep_delta=timedelta(hours=9), cognito_username=env["username"])
    _seed_session(worker, "owner", "11111111-1111-4111-8111-111111111111")

    result = worker.handle({"user_id": "owner"}, _FixedContext(900_000))
    assert result == {"status": "rescheduled"}
    assert worker._chat_metadata_repository().get("owner", "11111111-1111-4111-8111-111111111111") is not None
    assert worker._gate_repository().get("owner") is not None
    assert calls["checkpoint"] == [] and calls["namespace"] == []

    scheduler = boto3.client("scheduler", region_name="us-east-1")
    schedules = scheduler.list_schedules(GroupName=SCHEDULE_GROUP)["Schedules"]
    assert len(schedules) == 1


def test_on_time_run_deletes_every_store_for_this_user_only(env, monkeypatch):
    worker = _load_worker()
    calls = _patch_memory(monkeypatch, worker)
    sid1 = "11111111-1111-4111-8111-111111111111"
    sid2 = "22222222-2222-4222-8222-222222222222"
    _seed_session(worker, "owner", sid1, with_export=True, with_job=True)
    _seed_session(worker, "owner", sid2, with_export=True, with_job=True)
    other_sid = "33333333-3333-4333-8333-333333333333"
    _seed_session(worker, "other-user", other_sid, with_export=True, with_job=True)
    gate = _write_gate(worker, "owner", sweep_delta=timedelta(hours=-1), cognito_username=env["username"])

    result = worker.handle({"user_id": "owner"}, _FixedContext(900_000))
    assert result == {"status": "complete"}

    chat_metadata = worker._chat_metadata_repository()
    assert chat_metadata.get("owner", sid1) is None
    assert chat_metadata.get("owner", sid2) is None
    assert chat_metadata.get("other-user", other_sid) is not None  # another user's data is untouched

    history = boto3.resource("dynamodb", region_name="us-east-1").Table("ChatHistoryStore")
    assert history.get_item(Key={"session_id": sid1, "sequence_number": 0}).get("Item") is None
    assert history.get_item(Key={"session_id": other_sid, "sequence_number": 0}).get("Item") is not None

    jobs = worker._job_repository()
    assert jobs.list_for_user("owner", limit=10).items == []
    assert jobs.list_for_user("other-user", limit=10).items != []

    exports = worker._export_repository()
    assert exports.list_for_session("owner", sid1, limit=10).items == []
    assert exports.list_for_session("other-user", other_sid, limit=10).items != []
    s3 = boto3.client("s3", region_name="us-east-1")
    remaining = s3.list_objects_v2(Bucket="exports-bucket").get("Contents", [])
    assert len(remaining) == 1  # only the other user's export object survives

    secrets = boto3.client("secretsmanager", region_name="us-east-1")
    with pytest.raises(secrets.exceptions.ResourceNotFoundException):
        secrets.describe_secret(SecretId="cei/user-keys/owner/pubmed")

    # list_by_user's default order (newest activity first) is not asserted here -- only that both
    # sessions' checkpoints were deleted, each exactly once.
    assert sorted(calls["checkpoint"]) == sorted([(MEMORY_ID, sid1, "owner"), (MEMORY_ID, sid2, "owner")])
    assert calls["namespace"] == [(MEMORY_ID, "owner")]

    # User Metadata items (profile + gate) and the Cognito user are gone.
    assert worker._gate_repository().get("owner") is None
    cognito = boto3.client("cognito-idp", region_name="us-east-1")
    with pytest.raises(cognito.exceptions.UserNotFoundException):
        cognito.admin_get_user(UserPoolId=env["pool_id"], Username=env["username"])


def test_gate_is_removed_last_and_cognito_delete_is_last(env, monkeypatch):
    worker = _load_worker()
    _patch_memory(monkeypatch, worker)
    sid = "11111111-1111-4111-8111-111111111111"
    _seed_session(worker, "owner", sid)
    _write_gate(worker, "owner", sweep_delta=timedelta(hours=-1), cognito_username=env["username"])

    # Assert via call order on the three concrete boundary actions B109 orders explicitly: the "profile"
    # settings row, then the "deletion" gate row, then AdminDeleteUser.
    order = []
    from shared.persistence.account_gate import AccountGateRepository
    from shared.persistence.user_metadata import UserMetadataRepository

    original_delete_gate = AccountGateRepository.delete_gate
    original_delete_profile = UserMetadataRepository.delete

    def _record_delete_gate(self, user_id):
        order.append("gate")
        return original_delete_gate(self, user_id)

    def _record_delete_profile(self, user_id):
        order.append("profile")
        return original_delete_profile(self, user_id)

    monkeypatch.setattr(AccountGateRepository, "delete_gate", _record_delete_gate)
    monkeypatch.setattr(UserMetadataRepository, "delete", _record_delete_profile)

    cognito_client = boto3.client("cognito-idp", region_name="us-east-1")
    original_admin_delete_user = cognito_client.admin_delete_user

    def _record_admin_delete_user(**kwargs):
        order.append("cognito")
        return original_admin_delete_user(**kwargs)

    monkeypatch.setattr(worker, "_cognito_client", lambda: Mock(
        admin_delete_user=_record_admin_delete_user,
        exceptions=cognito_client.exceptions,
    ))

    result = worker.handle({"user_id": "owner"}, _FixedContext(900_000))
    assert result == {"status": "complete"}
    assert order == ["profile", "gate", "cognito"]


def test_rerun_after_partial_failure_completes(env, monkeypatch):
    worker = _load_worker()
    _patch_memory(monkeypatch, worker)
    sid1 = "11111111-1111-4111-8111-111111111111"
    sid2 = "22222222-2222-4222-8222-222222222222"
    _seed_session(worker, "owner", sid1)
    _seed_session(worker, "owner", sid2)
    _write_gate(worker, "owner", sweep_delta=timedelta(hours=-1), cognito_username=env["username"])

    # Fail only the very first per-session delete, whichever session list_by_user returns first; the
    # retry below must then complete both sessions regardless of that ordering.
    real_delete_session = worker._chat_metadata_repository().__class__.delete
    attempts = {"count": 0}

    def _flaky_delete(self, user_id, session_id):
        if attempts["count"] == 0:
            attempts["count"] += 1
            raise RuntimeError("transient failure")
        return real_delete_session(self, user_id, session_id)

    monkeypatch.setattr(worker._chat_metadata_repository().__class__, "delete", _flaky_delete)

    with pytest.raises(RuntimeError):
        worker.handle({"user_id": "owner"}, _FixedContext(900_000))

    # The exception aborts the whole per-session loop on the first session it reaches, so neither
    # session's metadata row is removed yet (that is each session's last step) -- but the gate survives
    # for a retry (it is never removed before every session has been cascaded).
    assert worker._chat_metadata_repository().get("owner", sid1) is not None
    assert worker._chat_metadata_repository().get("owner", sid2) is not None
    assert worker._gate_repository().get("owner") is not None

    # Retry: `attempts["count"]` is already 1, so `_flaky_delete` now behaves like the real delete for
    # every call -- including a harmless re-delete of the session that already succeeded.
    result = worker.handle({"user_id": "owner"}, _FixedContext(900_000))
    assert result == {"status": "complete"}
    assert worker._chat_metadata_repository().get("owner", sid1) is None
    assert worker._chat_metadata_repository().get("owner", sid2) is None
    assert worker._gate_repository().get("owner") is None
