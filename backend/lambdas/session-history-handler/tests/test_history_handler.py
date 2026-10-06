import base64
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

LAMBDAS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(LAMBDAS / "session-history-handler"))
sys.path.insert(0, str(LAMBDAS / "shared"))
sys.path.insert(0, str(LAMBDAS.parent / "agents"))

import history_handler as handler  # noqa: E402

NOW = datetime(2026, 10, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def tables(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="ChatMetadataStore",
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
        history = ddb.create_table(
            TableName="ChatHistoryStore",
            KeySchema=[
                {"AttributeName": "session_id", "KeyType": "HASH"},
                {"AttributeName": "sequence_number", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "session_id", "AttributeType": "S"},
                {"AttributeName": "sequence_number", "AttributeType": "N"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.Table("ChatMetadataStore").put_item(
            Item={
                "user_id": "user-a",
                "session_id": "s1",
                "title": "t",
                "status": "active",
                "message_count": 0,
                "created_at": NOW.isoformat(),
                "updated_at": NOW.isoformat(),
            }
        )
        for seq in range(1, 6):
            history.put_item(
                Item={
                    "session_id": "s1",
                    "sequence_number": seq,
                    "message_id": f"m{seq}",
                    "role": "human",
                    "content_blocks": [{"block_type": "text", "body": f"hi {seq}"}],
                    "citations": [],
                    "timestamp": NOW.isoformat(),
                    "tool_input": {"secret": 1},
                }
            )
        history.put_item(
            Item={"session_id": "s1", "sequence_number": 6, "record_type": "trace_event"}
        )
        handler._metadata = None
        handler._history = None
        yield


def call(sid="s1", *, sub="user-a", query=None, claims=True):
    event = {
        "httpMethod": "GET",
        "resource": "/sessions/{sessionId}/history",
        "pathParameters": {"sessionId": sid},
        "queryStringParameters": query,
        "requestContext": {"requestId": "req-1"},
    }
    if claims:
        event["requestContext"]["authorizer"] = {"claims": {"sub": sub}}
    response = handler.handler(event, None)
    return response["statusCode"], json.loads(response["body"])


def test_history_first_page_is_newest_turns_chronological():
    status, body = call(query={"limit": "2"})
    assert status == 200
    assert [i["sequence_number"] for i in body["items"]] == [4, 5]
    assert body["has_more"] is True and body["next_cursor"]
    item = body["items"][0]
    assert set(item) == {"message_id", "role", "content_blocks", "citations", "timestamp", "sequence_number"}


def test_history_cursor_round_trip_and_trace_items_skipped():
    seen = []
    cursor = None
    for _ in range(5):
        query = {"limit": "2", **({"cursor": cursor} if cursor else {})}
        _, body = call(query=query)
        seen = [i["sequence_number"] for i in body["items"]] + seen
        cursor = body["next_cursor"]
        if not body["has_more"]:
            break
    assert seen == [1, 2, 3, 4, 5]
    assert cursor is None


def test_empty_history_envelope():
    boto3.resource("dynamodb", region_name="us-east-1").Table("ChatMetadataStore").put_item(
        Item={
            "user_id": "user-a",
            "session_id": "empty",
            "title": "t",
            "status": "active",
            "message_count": 0,
            "created_at": NOW.isoformat(),
            "updated_at": NOW.isoformat(),
        }
    )
    assert call("empty") == (200, {"items": [], "next_cursor": None, "has_more": False})


def test_other_users_session_is_not_found():
    status, body = call(sub="user-b")
    assert status == 404 and body["error"]["code"] == "not_found"


def test_tampered_cursor_rejected():
    foreign = base64.urlsafe_b64encode(
        json.dumps({"session_id": "other-session", "sequence_number": 3}).encode()
    ).decode()
    for cursor in ("@@@", foreign, "e30"):
        status, body = call(query={"cursor": cursor})
        assert status == 400 and body["error"]["code"] == "invalid_request"


def test_malformed_input_is_invalid_request():
    assert call("bad id")[1]["error"]["code"] == "invalid_request"
    assert call(query={"limit": "101"})[1]["error"]["code"] == "invalid_request"


def test_missing_claims_is_unauthorized():
    status, body = call(claims=False)
    assert status == 401 and body["error"]["code"] == "unauthorized"
