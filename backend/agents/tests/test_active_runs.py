from unittest.mock import Mock, patch

import pytest
from botocore.exceptions import ClientError

from shared.persistence.active_runs import ActiveRunRepository, ConcurrencyLimitError, RunLease


def rejected():
    return ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")


def test_acquire_uses_second_slot_after_conflict():
    table = Mock()
    table.update_item.side_effect = [rejected(), {}]
    with patch("shared.persistence.active_runs.time.time", return_value=100):
        lease = ActiveRunRepository(table).acquire("user", "session", "run", ("scope",))
    assert lease == RunLease("user", "session", "run", 1, 1060, ("scope",))
    second = table.update_item.call_args_list[1].kwargs
    assert second["ExpressionAttributeNames"]["#slot"] == "slot_1"
    assert second["ExpressionAttributeNames"]["#other"] == "slot_0"
    assert "#other.#session <> :session" in second["ConditionExpression"]


def test_two_conditional_failures_raise_limit():
    table = Mock()
    table.update_item.side_effect = [rejected(), rejected()]
    with pytest.raises(ConcurrencyLimitError):
        ActiveRunRepository(table).acquire("user", "session", "run", ("scope",))


def test_release_cannot_remove_replacement_run():
    table = Mock()
    table.update_item.side_effect = rejected()
    lease = RunLease("user", "session", "old-run", 0, 500, ("scope",))
    assert not ActiveRunRepository(table).release(lease)
    assert table.update_item.call_args.kwargs["ExpressionAttributeValues"][":run"] == "old-run"


def test_find_ignores_expired_lease_and_uses_consistent_read():
    table = Mock()
    table.get_item.return_value = {"Item": {"slot_0": {
        "session_id": "session", "run_id": "run", "expires_at": 100, "scopes": ["scope"]
    }}}
    with patch("shared.persistence.active_runs.time.time", return_value=100):
        assert ActiveRunRepository(table).find("user", "session") is None
    assert table.get_item.call_args.kwargs["ConsistentRead"] is True
