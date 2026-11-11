from unittest.mock import Mock, call

import pytest

from shared.persistence.replay_deletion import delete_session_replays

SESSION = "6f1c2d3e-0000-4000-8000-000000000001"
RUN1 = "6f1c2d3e-0000-4000-8000-000000000002"
RUN2 = "6f1c2d3e-0000-4000-8000-000000000003"


def row(run):
    return {"user_id": "alice", "record": f"replay#{SESSION}#{run}",
            "history_partition": f"sse#alice#{SESSION}#{run}"}


def test_deletes_all_pages_and_indexes_after_partitions():
    table, history = Mock(), Mock()
    cursor = {"user_id": "alice", "record": row(RUN1)["record"]}
    table.query.side_effect = [{"Items": [row(RUN1)], "LastEvaluatedKey": cursor}, {"Items": [row(RUN2)]}]
    delete_session_replays(user_id="alice", session_id=SESSION, runs_table=table, history=history)
    assert history.delete_session.call_args_list == [call(row(RUN1)["history_partition"]),
                                                     call(row(RUN2)["history_partition"])]
    assert table.query.call_args_list[1].kwargs["ExclusiveStartKey"] == cursor
    assert table.delete_item.call_count == 2
    assert all(x.kwargs["ConditionExpression"] == "history_partition = :partition" for x in table.delete_item.call_args_list)


def test_forged_partition_is_never_deleted():
    table, history = Mock(), Mock()
    table.query.return_value = {"Items": [{**row(RUN1), "history_partition": "sse#bob#other"}]}
    with pytest.raises(ValueError):
        delete_session_replays(user_id="alice", session_id=SESSION, runs_table=table, history=history)
    history.delete_session.assert_not_called()
    table.delete_item.assert_not_called()


def test_failure_keeps_index_for_retry():
    table, history = Mock(), Mock()
    table.query.return_value = {"Items": [row(RUN1)]}
    history.delete_session.side_effect = RuntimeError("storage unavailable")
    with pytest.raises(RuntimeError):
        delete_session_replays(user_id="alice", session_id=SESSION, runs_table=table, history=history)
    table.delete_item.assert_not_called()
