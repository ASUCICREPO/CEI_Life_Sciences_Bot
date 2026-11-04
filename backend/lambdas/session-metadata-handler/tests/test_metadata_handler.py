import base64
import json
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

LAMBDAS = Path(__file__).resolve().parents[2]
# ADR: tests import `shared.persistence` from backend/agents (the layer ships a subset of it) and `http_errors` from lambdas/shared
sys.path.insert(0, str(LAMBDAS / "session-metadata-handler"))
sys.path.insert(0, str(LAMBDAS / "shared"))
sys.path.insert(0, str(LAMBDAS.parent / "agents"))

import metadata_handler as handler  # noqa: E402

ORIGIN = "https://site.example.net"


@pytest.fixture(autouse=True)
def tables(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("ALLOWED_ORIGINS", ORIGIN)
    with mock_aws():
        boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="ChatMetadataStore",
            KeySchema=[
                {"AttributeName": "user_id", "KeyType": "HASH"},
                {"AttributeName": "session_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "user_id", "AttributeType": "S"},
                {"AttributeName": "session_id", "AttributeType": "S"},
                {"AttributeName": "created_at", "AttributeType": "S"},
                {"AttributeName": "updated_at", "AttributeType": "S"},
            ],
            # mirrors backend/lib/backend-stack.ts (ChatMetadataStore GSIs)
            GlobalSecondaryIndexes=[
                {
                    "IndexName": name,
                    "KeySchema": [
                        {"AttributeName": "user_id", "KeyType": "HASH"},
                        {"AttributeName": sk, "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
                for name, sk in (("by_created", "created_at"), ("by_updated", "updated_at"))
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        handler._repo = None
        yield


def call(method, resource, *, sub="user-a", body=None, path=None, query=None, claims=True):
    event = {
        "httpMethod": method,
        "resource": resource,
        "pathParameters": path,
        "queryStringParameters": query,
        "headers": {"Origin": ORIGIN},
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {"requestId": "req-1"},
    }
    if claims:
        event["requestContext"]["authorizer"] = {"claims": {"sub": sub}}
    response = handler.handler(event, None)
    return response["statusCode"], json.loads(response["body"]), response


def create(sub="user-a", title="Chat", mode="science", **extra):
    status, body, _ = call("POST", "/sessions", sub=sub, body={"title": title, "mode": mode, **extra})
    assert status == 201
    return body


def test_create_then_list_and_get():
    created = create(model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0")
    assert created["status"] == "active" and created["mode"] == "science"
    assert set(created) == {"session_id", "title", "title_source", "mode", "status", "created_at"}
    assert len(created["session_id"]) >= 33  # valid AgentCore runtimeSessionId

    status, listed, _ = call("GET", "/sessions")
    assert status == 200
    assert [i["session_id"] for i in listed["items"]] == [created["session_id"]]
    assert listed["next_cursor"] is None and listed["has_more"] is False
    assert "user_id" not in listed["items"][0]
    # turn context is returned alongside model_id; a POST-created session has none yet
    assert listed["items"][0]["document_type"] is None and listed["items"][0]["analysis_type"] is None

    status, one, _ = call("GET", "/sessions/{sessionId}", path={"sessionId": created["session_id"]})
    assert status == 200 and one["title"] == "Chat" and one["model_id"].startswith("us.anthropic")
    assert one["document_type"] is None and one["analysis_type"] is None


def test_list_and_get_return_stored_turn_context():
    boto3.resource("dynamodb", region_name="us-east-1").Table("ChatMetadataStore").put_item(
        Item={
            "user_id": "user-a",
            "session_id": "s" * 33,
            "title": "Draft",
            "status": "active",
            "message_count": 0,
            "agent_type": "drafting",
            "document_type": "grant",
            "analysis_type": "competitor",
            "created_at": "2026-01-01T00:00:00.000000Z",
            "updated_at": "2026-01-01T00:00:00.000000Z",
        }
    )
    _, listed, _ = call("GET", "/sessions")
    status, one, _ = call("GET", "/sessions/{sessionId}", path={"sessionId": "s" * 33})
    assert status == 200
    for body in (listed["items"][0], one):
        assert body["agent_type"] == "drafting"
        assert body["document_type"] == "grant"
        assert body["analysis_type"] == "competitor"
        assert "user_id" not in body


def test_clients_cannot_set_turn_context():
    status, _, _ = call("POST", "/sessions", body={"title": "t", "mode": "science", "document_type": "x"})
    assert status == 400
    sid = create()["session_id"]
    status, _, _ = call("PATCH", "/sessions/{sessionId}", path={"sessionId": sid}, body={"analysis_type": "x"})
    assert status == 400


def test_patch_updates_title_and_mode():
    sid = create()["session_id"]
    status, body, _ = call(
        "PATCH", "/sessions/{sessionId}", path={"sessionId": sid}, body={"title": " New ", "mode": "business"}
    )
    assert status == 200 and body["title"] == "New" and body["mode"] == "business"
    assert body["title_source"] == "user"
    _, again, _ = call("GET", "/sessions/{sessionId}", path={"sessionId": sid})
    assert again["title"] == "New" and again["mode"] == "business"


def test_patch_mode_only_keeps_title_source():
    sid = create()["session_id"]
    _, body, _ = call("PATCH", "/sessions/{sessionId}", path={"sessionId": sid}, body={"mode": "business"})
    assert body["title_source"] == "prompt"


def test_list_sorts_by_updated_by_default_or_created_on_request():
    first = create(title="first")["session_id"]
    second = create(title="second")["session_id"]
    # touching `first` makes it the most recently updated
    call("PATCH", "/sessions/{sessionId}", path={"sessionId": first}, body={"mode": "business"})
    _, by_updated, _ = call("GET", "/sessions")
    assert [i["session_id"] for i in by_updated["items"]] == [first, second]
    _, explicit, _ = call("GET", "/sessions", query={"sort": "updated"})
    assert explicit["items"] == by_updated["items"]
    _, by_created, _ = call("GET", "/sessions", query={"sort": "created"})
    assert [i["session_id"] for i in by_created["items"]] == [second, first]
    assert {"title", "title_source"} <= set(by_created["items"][0])


def test_invalid_sort_is_rejected():
    status, body, _ = call("GET", "/sessions", query={"sort": "title"})
    assert status == 400 and body["error"]["code"] == "invalid_request"


def test_cursor_from_other_sort_order_is_rejected():
    for i in range(2):
        create(title=f"c{i}")
    _, page, _ = call("GET", "/sessions", query={"limit": "1", "sort": "created"})
    status, body, _ = call("GET", "/sessions", query={"limit": "1", "sort": "updated", "cursor": page["next_cursor"]})
    assert status == 400 and body["error"]["code"] == "invalid_request"
    status, _, _ = call("GET", "/sessions", query={"limit": "1", "sort": "created", "cursor": page["next_cursor"]})
    assert status == 200


def test_cross_user_isolation():
    sid = create(sub="user-a")["session_id"]
    assert call("GET", "/sessions", sub="user-b")[1]["items"] == []
    assert call("GET", "/sessions/{sessionId}", sub="user-b", path={"sessionId": sid})[0] == 404
    status, _, _ = call(
        "PATCH", "/sessions/{sessionId}", sub="user-b", path={"sessionId": sid}, body={"title": "x"}
    )
    assert status == 404
    assert call("GET", "/sessions/{sessionId}", sub="user-a", path={"sessionId": sid})[1]["title"] == "Chat"


def test_user_id_in_body_is_rejected_not_trusted():
    status, body, _ = call(
        "POST", "/sessions", body={"title": "t", "mode": "science", "user_id": "victim"}
    )
    assert status == 400 and body["error"]["code"] == "invalid_request"


def test_cursor_round_trip():
    ids = [create(title=f"c{i}")["session_id"] for i in range(3)]
    _, first, _ = call("GET", "/sessions", query={"limit": "2"})
    assert len(first["items"]) == 2 and first["has_more"] is True
    _, second, _ = call("GET", "/sessions", query={"limit": "2", "cursor": first["next_cursor"]})
    seen = [i["session_id"] for i in first["items"] + second["items"]]
    assert sorted(seen) == sorted(ids) and len(set(seen)) == 3
    assert second["has_more"] is False and second["next_cursor"] is None


def test_tampered_cursor_is_rejected():
    create(sub="user-a")
    create(sub="user-b")
    _, page, _ = call("GET", "/sessions", sub="user-b", query={"limit": "1"})
    other = base64.urlsafe_b64encode(
        json.dumps({"user_id": "user-b", "session_id": "x", "updated_at": "2026-01-01T00:00:00.000000Z"}).encode()
    ).decode()
    legacy_shape = base64.urlsafe_b64encode(json.dumps({"user_id": "user-a", "session_id": "x"}).encode()).decode()
    for cursor in ("not-base64!!", "e30", other, legacy_shape):
        status, body, _ = call("GET", "/sessions", sub="user-a", query={"cursor": cursor})
        assert status == 400 and body["error"]["code"] == "invalid_request"


@pytest.mark.parametrize(
    "payload",
    [
        {"mode": "science"},
        {"title": "", "mode": "science"},
        {"title": "x" * 201, "mode": "science"},
        {"title": 5, "mode": "science"},
        {"title": "ok", "mode": "other"},
        {"title": "ok", "mode": "science", "model_id": "bad id!"},
    ],
)
def test_malformed_create_is_invalid_request(payload):
    status, body, _ = call("POST", "/sessions", body=payload)
    assert status == 400
    assert body["error"]["code"] == "invalid_request"
    assert body["error"]["retriable"] is False and body["error"]["request_id"] == "req-1"


def test_malformed_bodies_and_params():
    assert call("POST", "/sessions")[1]["error"]["code"] == "invalid_request"
    sid = create()["session_id"]
    assert call("PATCH", "/sessions/{sessionId}", path={"sessionId": sid}, body={})[0] == 400
    assert call("GET", "/sessions/{sessionId}", path={"sessionId": "bad id"})[0] == 400
    for limit in ("0", "101", "abc"):
        assert call("GET", "/sessions", query={"limit": limit})[0] == 400


def test_missing_claims_is_unauthorized():
    status, body, _ = call("GET", "/sessions", claims=False)
    assert status == 401 and body["error"]["code"] == "unauthorized"


def test_cors_echoes_only_allowed_origin():
    _, _, response = call("GET", "/sessions")
    assert response["headers"]["Access-Control-Allow-Origin"] == ORIGIN
    event = {
        "httpMethod": "GET",
        "resource": "/sessions",
        "headers": {"Origin": "https://evil.example"},
        "requestContext": {"authorizer": {"claims": {"sub": "u"}}},
    }
    assert "Access-Control-Allow-Origin" not in handler.handler(event, None)["headers"]
