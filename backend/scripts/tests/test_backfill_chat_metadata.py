import sys

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

import backfill_chat_metadata as bf
from shared.persistence.chat_metadata import normalize_title

TABLE = "ChatMetadataStore"
REGION = "us-east-1"


@pytest.fixture
def table(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=REGION)
        t = ddb.create_table(
            TableName=TABLE,
            KeySchema=[
                {"AttributeName": "user_id", "KeyType": "HASH"},
                {"AttributeName": "session_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "user_id", "AttributeType": "S"},
                {"AttributeName": "session_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield t


def _get(table, sid, uid="u1"):
    return table.get_item(Key={"user_id": uid, "session_id": sid}).get("Item")


def _seed(table):
    table.put_item(Item={"user_id": "u1", "session_id": "legacy", "title": "  Hello   WORLD ", "updated_at": "2024-01-02T00:00:00Z"})
    table.put_item(
        Item={
            "user_id": "u1",
            "session_id": "partial",
            "title": "Some Title",
            "updated_at": "2024-02-02T00:00:00Z",
            "last_activity_at": "2024-03-03T00:00:00Z",
        }
    )
    table.put_item(
        Item={
            "user_id": "u1",
            "session_id": "done",
            "title": "Done",
            "updated_at": "2024-04-04T00:00:00Z",
            "last_activity_at": "2024-05-05T00:00:00Z",
            "title_search": "custom-keep",
        }
    )
    table.put_item(Item={"user_id": "u2", "session_id": "notitle", "updated_at": "2024-06-06T00:00:00Z"})


def test_dry_run_writes_nothing(table):
    _seed(table)
    before = sorted(table.scan()["Items"], key=lambda i: i["session_id"])

    scanned, changed = bf.backfill(table, apply=False)

    assert (scanned, changed) == (4, 3)
    assert sorted(table.scan()["Items"], key=lambda i: i["session_id"]) == before


def test_apply_fills_only_missing_fields(table):
    _seed(table)

    scanned, changed = bf.backfill(table, apply=True)

    assert (scanned, changed) == (4, 3)
    legacy = _get(table, "legacy")
    assert legacy["last_activity_at"] == "2024-01-02T00:00:00Z"
    assert legacy["title_search"] == normalize_title("  Hello   WORLD ") == "hello world"

    partial = _get(table, "partial")
    assert partial["last_activity_at"] == "2024-03-03T00:00:00Z"  # existing value untouched
    assert partial["title_search"] == "some title"

    done = _get(table, "done")
    assert done["last_activity_at"] == "2024-05-05T00:00:00Z"
    assert done["title_search"] == "custom-keep"

    notitle = _get(table, "notitle", uid="u2")
    assert notitle["title_search"] == ""
    assert notitle["last_activity_at"] == "2024-06-06T00:00:00Z"


def test_apply_is_idempotent(table):
    _seed(table)
    bf.backfill(table, apply=True)
    assert bf.backfill(table, apply=True) == (4, 0)


def test_row_without_updated_at_gets_no_last_activity(table):
    table.put_item(Item={"user_id": "u1", "session_id": "s", "title": "T"})
    bf.backfill(table, apply=True)
    item = _get(table, "s")
    assert "last_activity_at" not in item
    assert item["title_search"] == "t"


class _DeleteAfterScan:
    """Delegates to the moto table but deletes one row between scan and update (deleted mid-scan)."""

    def __init__(self, table, victim):
        self._table = table
        self._victim = victim
        self.update_calls = 0

    def scan(self, **kwargs):
        page = self._table.scan(**kwargs)
        self._table.delete_item(Key=self._victim)
        return page

    def update_item(self, **kwargs):
        self.update_calls += 1
        return self._table.update_item(**kwargs)


def test_row_deleted_mid_scan_is_skipped(table):
    _seed(table)
    victim = {"user_id": "u1", "session_id": "legacy"}
    wrapped = _DeleteAfterScan(table, victim)

    scanned, changed = bf.backfill(wrapped, apply=True)

    assert (scanned, changed) == (4, 3)
    assert wrapped.update_calls == 3
    assert _get(table, "legacy") is None  # not resurrected by the update
    assert _get(table, "partial")["title_search"] == "some title"


class _StubTable:
    def __init__(self, pages, error_code=None):
        self.pages = list(pages)
        self.scan_kwargs = []
        self.updates = []
        self.error_code = error_code

    def scan(self, **kwargs):
        self.scan_kwargs.append(dict(kwargs))
        return self.pages.pop(0)

    def update_item(self, **kwargs):
        self.updates.append(kwargs)
        if self.error_code:
            raise ClientError({"Error": {"Code": self.error_code, "Message": "x"}}, "UpdateItem")


def test_other_client_errors_propagate():
    stub = _StubTable([{"Items": [{"user_id": "u", "session_id": "s", "title": "T"}]}], error_code="ProvisionedThroughputExceededException")
    with pytest.raises(ClientError):
        bf.backfill(stub, apply=True)


def test_follows_pagination():
    stub = _StubTable(
        [
            {"Items": [{"user_id": "u", "session_id": "a", "title": "A"}], "LastEvaluatedKey": {"user_id": "u", "session_id": "a"}},
            {"Items": [{"user_id": "u", "session_id": "b", "title": "B"}]},
        ]
    )
    assert bf.backfill(stub, apply=True) == (2, 2)
    assert "ExclusiveStartKey" not in stub.scan_kwargs[0]
    assert stub.scan_kwargs[1]["ExclusiveStartKey"] == {"user_id": "u", "session_id": "a"}
    upd = stub.updates[0]
    assert upd["ConditionExpression"] == "attribute_exists(session_id)"
    assert "if_not_exists" in upd["UpdateExpression"]


def test_table_arg_is_required(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["backfill_chat_metadata.py"])
    with pytest.raises(SystemExit) as ei:
        bf.main()
    assert ei.value.code == 2
    assert "--table" in capsys.readouterr().err


def test_main_defaults_to_dry_run(table, monkeypatch, capsys):
    _seed(table)
    monkeypatch.setattr(sys, "argv", ["backfill_chat_metadata.py", "--table", TABLE, "--region", REGION])
    assert bf.main() == 0
    assert "would update 3" in capsys.readouterr().out
    assert "title_search" not in _get(table, "legacy")


def test_main_apply_writes(table, monkeypatch, capsys):
    _seed(table)
    monkeypatch.setattr(sys, "argv", ["backfill_chat_metadata.py", "--table", TABLE, "--region", REGION, "--apply"])
    assert bf.main() == 0
    assert "scanned 4 rows; updated 3" in capsys.readouterr().out
    assert _get(table, "legacy")["title_search"] == "hello world"
