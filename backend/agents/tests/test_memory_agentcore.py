"""F19/B109: the two account-delete cascade helpers added to shared/memory/agentcore.py.

AgentCore Memory has no moto backend, so these tests use a Mock boto3 client / a Mock checkpointer
factory rather than real AWS calls.
"""
from __future__ import annotations

from unittest.mock import Mock, call, patch

from shared.memory import agentcore


def test_delete_checkpoint_calls_delete_thread_with_the_session_as_actor():
    saver = Mock()
    with patch.object(agentcore, "create_checkpointer", return_value=saver) as create:
        agentcore.delete_checkpoint("memory-1", "session-1", "user-1")
    create.assert_called_once_with("memory-1")
    saver.delete_thread.assert_called_once_with("session-1", "user-1")


def test_delete_long_term_namespace_deletes_every_page_of_records():
    client = Mock()
    client.list_memory_records.side_effect = [
        {"memoryRecordSummaries": [{"memoryRecordId": "r1"}, {"memoryRecordId": "r2"}], "nextToken": "page2"},
        {"memoryRecordSummaries": [{"memoryRecordId": "r3"}], "nextToken": None},
    ]
    agentcore.delete_long_term_namespace("memory-1", "user-1", client=client)

    assert client.list_memory_records.call_args_list == [
        call(memoryId="memory-1", namespace="/users/user-1/", maxResults=100),
        call(memoryId="memory-1", namespace="/users/user-1/", maxResults=100, nextToken="page2"),
    ]
    assert client.batch_delete_memory_records.call_args_list == [
        call(memoryId="memory-1", records=[{"memoryRecordId": "r1"}, {"memoryRecordId": "r2"}]),
        call(memoryId="memory-1", records=[{"memoryRecordId": "r3"}]),
    ]


def test_delete_long_term_namespace_is_a_no_op_when_empty():
    client = Mock()
    client.list_memory_records.return_value = {"memoryRecordSummaries": []}
    agentcore.delete_long_term_namespace("memory-1", "user-1", client=client)
    client.batch_delete_memory_records.assert_not_called()
