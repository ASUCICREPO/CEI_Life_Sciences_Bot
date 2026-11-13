from datetime import UTC, datetime
from unittest.mock import Mock

import pytest

from shared.memory.long_term import fetch_long_term_context, record_conversation, user_namespace


def test_retrieval_uses_exact_user_namespace_and_bounded_results():
    client = Mock()
    client.retrieve_memory_records.return_value = {"memoryRecordSummaries": [
        {"content": {"text": "prefers APA"}, "namespaces": ["/users/alice/"]}]}
    assert fetch_long_term_context(client=client, memory_id="memory", user_id="alice", query="citations") == ["prefers APA"]
    args = client.retrieve_memory_records.call_args.kwargs
    assert args["namespace"] == "/users/alice/" and "namespacePath" not in args
    assert args["searchCriteria"]["topK"] == 10


@pytest.mark.parametrize("user", ["../bob", "alice/bob", "*", "", "a:b"])
def test_user_namespace_rejects_path_injection(user):
    with pytest.raises(ValueError):
        user_namespace(user)


def test_unexpected_cross_user_record_rejected():
    client = Mock()
    client.retrieve_memory_records.return_value = {"memoryRecordSummaries": [
        {"content": {"text": "private"}, "namespaces": ["/users/bob/"]}]}
    with pytest.raises(ValueError):
        fetch_long_term_context(client=client, memory_id="memory", user_id="alice", query="research")


def test_empty_configuration_never_calls_aws():
    client = Mock()
    assert fetch_long_term_context(client=client, memory_id=None, user_id="alice", query="test") == []
    record_conversation(client=client, memory_id=None, user_id="alice", session_id="chat", user_message="hi",
                        assistant_message="hello", event_time=datetime.now(UTC), idempotency_token="turn")
    client.assert_not_called()


def test_turn_event_uses_verified_actor_and_stable_idempotency_token():
    client = Mock()
    when = datetime(2026, 1, 1, tzinfo=UTC)
    record_conversation(client=client, memory_id="memory", user_id="alice", session_id="chat", user_message="hi",
                        assistant_message="hello", event_time=when, idempotency_token="turn-123")
    args = client.create_event.call_args.kwargs
    assert (args["actorId"], args["sessionId"], args["clientToken"]) == ("alice", "chat", "turn-123")
    assert [part["conversational"]["role"] for part in args["payload"]] == ["USER", "ASSISTANT"]


def test_aws_failure_propagates():
    client = Mock()
    client.retrieve_memory_records.side_effect = RuntimeError("service unavailable")
    with pytest.raises(RuntimeError):
        fetch_long_term_context(client=client, memory_id="memory", user_id="alice", query="test")
