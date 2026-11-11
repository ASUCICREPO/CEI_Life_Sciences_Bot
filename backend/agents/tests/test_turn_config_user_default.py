"""F8 follow-up: resolve_turn_config's owner-scoped saved-default lookup for a chat with no
stored model_id (shared/runtime/turn_config.py). Complements test_turn_config.py.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import boto3
import pytest
from moto import mock_aws

from shared.config import config
from shared.persistence.user_metadata import UserMetadataRepository
from shared.runtime.turn_config import resolve_turn_config

SAVED_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
BLOCKED_MODEL_ID = "anthropic.claude-fable-5-v1"


@pytest.fixture
def user_metadata_table(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    # Presence of the env var is what resolve_turn_config checks before reading (local runs skip it).
    monkeypatch.setenv("CEI_USER_METADATA_TABLE", config.user_metadata_table)
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=config.aws_region)
        ddb.create_table(
            TableName=config.user_metadata_table,
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
        yield UserMetadataRepository()


def test_saved_default_used_when_no_stored_model(user_metadata_table):
    user_metadata_table.put("owner", default_model_id=SAVED_MODEL_ID, default_mode=None)
    initial = SimpleNamespace(mode=None, model_id=None)
    final = SimpleNamespace(mode="science", model_id=SAVED_MODEL_ID)
    recorder = SimpleNamespace(session_metadata=AsyncMock(return_value=initial),
                               initialize_turn_settings=AsyncMock(return_value=final))
    result = asyncio.run(resolve_turn_config(recorder, user_id="owner", session_id="session", initial_mode="science"))
    assert result.model_id == SAVED_MODEL_ID
    recorder.initialize_turn_settings.assert_awaited_once_with(
        user_id="owner", session_id="session", mode="science", model_id=SAVED_MODEL_ID,
    )


def test_blocked_saved_default_falls_back_to_global_default_without_raising(user_metadata_table):
    user_metadata_table.put("owner", default_model_id=BLOCKED_MODEL_ID, default_mode=None)
    initial = SimpleNamespace(mode="science", model_id=None)
    final = SimpleNamespace(mode="science", model_id=config.default_model_id)
    recorder = SimpleNamespace(session_metadata=AsyncMock(return_value=initial),
                               initialize_turn_settings=AsyncMock(return_value=final))
    result = asyncio.run(resolve_turn_config(recorder, user_id="owner", session_id="session", initial_mode="science"))
    assert result.model_id == config.default_model_id


def test_saved_default_is_owner_scoped(user_metadata_table):
    user_metadata_table.put("bob", default_model_id=SAVED_MODEL_ID, default_mode=None)
    initial = SimpleNamespace(mode=None, model_id=None)
    final = SimpleNamespace(mode="science", model_id=config.default_model_id)
    recorder = SimpleNamespace(session_metadata=AsyncMock(return_value=initial),
                               initialize_turn_settings=AsyncMock(return_value=final))
    result = asyncio.run(resolve_turn_config(recorder, user_id="alice", session_id="session", initial_mode=None))
    assert result.model_id == config.default_model_id


def test_no_saved_profile_falls_back_to_global_default(user_metadata_table):
    initial = SimpleNamespace(mode=None, model_id=None)
    final = SimpleNamespace(mode="science", model_id=config.default_model_id)
    recorder = SimpleNamespace(session_metadata=AsyncMock(return_value=initial),
                               initialize_turn_settings=AsyncMock(return_value=final))
    result = asyncio.run(resolve_turn_config(recorder, user_id="owner", session_id="session", initial_mode=None))
    assert result.model_id == config.default_model_id


def test_missing_table_env_var_skips_read_and_uses_global_default(monkeypatch):
    monkeypatch.delenv("CEI_USER_METADATA_TABLE", raising=False)
    initial = SimpleNamespace(mode=None, model_id=None)
    final = SimpleNamespace(mode="science", model_id=config.default_model_id)
    recorder = SimpleNamespace(session_metadata=AsyncMock(return_value=initial),
                               initialize_turn_settings=AsyncMock(return_value=final))
    result = asyncio.run(resolve_turn_config(recorder, user_id="owner", session_id="session", initial_mode=None))
    assert result.model_id == config.default_model_id


def test_read_error_falls_back_to_global_default_without_leaking_details(monkeypatch):
    monkeypatch.setenv("CEI_USER_METADATA_TABLE", config.user_metadata_table)

    class RaisingRepo:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("boom: connection reset")

    monkeypatch.setattr("shared.persistence.user_metadata.UserMetadataRepository", RaisingRepo)
    initial = SimpleNamespace(mode=None, model_id=None)
    final = SimpleNamespace(mode="science", model_id=config.default_model_id)
    recorder = SimpleNamespace(session_metadata=AsyncMock(return_value=initial),
                               initialize_turn_settings=AsyncMock(return_value=final))
    result = asyncio.run(resolve_turn_config(recorder, user_id="owner", session_id="session", initial_mode=None))
    assert result.model_id == config.default_model_id


def test_saved_default_lookup_happens_once_per_call_across_retries(user_metadata_table, monkeypatch):
    # Race-winner retry (same shape as test_turn_config.test_legacy_settings_are_initialized_and_race_winner_is_rechecked),
    # but asserting the saved-default GetItem is not repeated on each retry iteration (ADR in turn_config.py).
    user_metadata_table.put("owner", default_model_id=SAVED_MODEL_ID, default_mode=None)
    calls: list[str] = []
    original_get = UserMetadataRepository.get

    def counting_get(self, user_id):
        calls.append(user_id)
        return original_get(self, user_id)

    monkeypatch.setattr(UserMetadataRepository, "get", counting_get)
    initial = SimpleNamespace(mode=None, model_id=None)
    concurrent = SimpleNamespace(mode="science", model_id=None)
    final = SimpleNamespace(mode="science", model_id=SAVED_MODEL_ID)
    recorder = SimpleNamespace(session_metadata=AsyncMock(return_value=initial),
                               initialize_turn_settings=AsyncMock(side_effect=[concurrent, final]))
    result = asyncio.run(resolve_turn_config(recorder, user_id="owner", session_id="session", initial_mode="business"))
    assert result.mode == "science" and result.model_id == SAVED_MODEL_ID
    assert calls == ["owner"]
