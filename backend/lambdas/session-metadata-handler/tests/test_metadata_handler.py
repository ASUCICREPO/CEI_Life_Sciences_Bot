import base64
import json
import sys
import uuid
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
GENERIC_500 = "It's not you, it's us. Something went wrong"
LEGACY_SID = "6f1c2d3e-0000-4000-8000-0000000000aa"


@pytest.fixture(autouse=True)
def tables(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("ALLOWED_ORIGINS", ORIGIN)
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
                {"AttributeName": "created_at", "AttributeType": "S"},
                {"AttributeName": "updated_at", "AttributeType": "S"},
                {"AttributeName": "last_activity_at", "AttributeType": "S"},
                {"AttributeName": "title_search", "AttributeType": "S"},
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
                for name, sk in (
                    ("by_created", "created_at"),
                    ("by_updated", "updated_at"),
                    ("by_activity", "last_activity_at"),
                    ("by_title", "title_search"),
                )
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        # GET/PATCH /sessions/{id} prove ownership against the history partition's stamped owner first
        ddb.create_table(
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
        handler._repo = None
        handler._history = None
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


def user_message(sub: str, sid: str) -> None:
    """What a runtime turn does to the metadata row when the user sends a message."""
    handler._repository().increment_message_count(sub, sid, user_activity=True)


def ids(page) -> list[str]:
    return [i["session_id"] for i in page["items"]]


def test_create_then_list_and_get():
    created = create(model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0")
    assert created["status"] == "active" and created["mode"] == "science"
    assert set(created) == {"session_id", "title", "title_source", "mode", "status", "created_at"}
    # a dedicated UUID (also a valid AgentCore runtimeSessionId: >= 33 chars)
    assert str(uuid.UUID(created["session_id"])) == created["session_id"]

    status, listed, _ = call("GET", "/sessions")
    assert status == 200
    assert ids(listed) == [created["session_id"]]
    assert listed["next_cursor"] is None and listed["has_more"] is False
    assert "user_id" not in listed["items"][0] and "title_search" not in listed["items"][0]
    assert listed["items"][0]["last_activity_at"]
    # turn context is returned alongside model_id; a POST-created session has none yet
    assert listed["items"][0]["document_type"] is None and listed["items"][0]["analysis_type"] is None

    status, one, _ = call("GET", "/sessions/{sessionId}", path={"sessionId": created["session_id"]})
    assert status == 200 and one["title"] == "Chat" and one["model_id"].startswith("us.anthropic")
    assert one["document_type"] is None and one["analysis_type"] is None


def test_created_session_ids_are_unique_uuids():
    created = {create(title=f"c{i}")["session_id"] for i in range(5)}
    assert len(created) == 5 and all(uuid.UUID(s).version == 4 for s in created)


def test_list_and_get_return_stored_turn_context():
    boto3.resource("dynamodb", region_name="us-east-1").Table("ChatMetadataStore").put_item(
        Item={
            "user_id": "user-a",
            "session_id": LEGACY_SID,
            "title": "Draft",
            "status": "active",
            "message_count": 0,
            "agent_type": "drafting",
            "document_type": "grant",
            "analysis_type": "competitor",
            "created_at": "2026-01-01T00:00:00.000000Z",
            "updated_at": "2026-01-01T00:00:00.000000Z",
            "last_activity_at": "2026-01-01T00:00:00.000000Z",
            "title_search": "draft",
        }
    )
    _, listed, _ = call("GET", "/sessions")
    status, one, _ = call("GET", "/sessions/{sessionId}", path={"sessionId": LEGACY_SID})
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


def test_list_defaults_to_last_user_activity():
    old = create(title="old")["session_id"]
    new = create(title="new")["session_id"]
    _, page, _ = call("GET", "/sessions")
    assert ids(page) == [new, old]

    user_message("user-a", old)  # the user writes in the old chat: it moves to the top
    _, page, _ = call("GET", "/sessions")
    assert ids(page) == [old, new]
    _, explicit, _ = call("GET", "/sessions", query={"sort": "activity"})
    assert explicit["items"] == page["items"]

    # a rename is not "use": it bumps updated_at but not the default order
    call("PATCH", "/sessions/{sessionId}", path={"sessionId": new}, body={"title": "renamed"})
    assert ids(call("GET", "/sessions")[1]) == [old, new]
    assert ids(call("GET", "/sessions", query={"sort": "updated"})[1]) == [new, old]
    _, by_created, _ = call("GET", "/sessions", query={"sort": "created"})
    assert ids(by_created) == [new, old]
    assert {"title", "title_source"} <= set(by_created["items"][0])


def test_default_page_size_is_25_with_cursor_to_the_next_page():
    made = [create(title=f"chat {i}")["session_id"] for i in range(30)]
    _, first, _ = call("GET", "/sessions")
    assert len(first["items"]) == 25 and first["has_more"] is True
    _, second, _ = call("GET", "/sessions", query={"cursor": first["next_cursor"]})
    assert len(second["items"]) == 5 and second["has_more"] is False
    assert sorted(ids(first) + ids(second)) == sorted(made)


def test_search_matches_title_substrings_case_insensitively_for_the_caller_only():
    kras = create(title="KRAS G12C resistance")["session_id"]
    create(title="Grant draft")
    create(sub="user-b", title="kras for someone else")
    for q in ("kras", "  KRAS  ", "resist", "g12c res"):
        status, page, _ = call("GET", "/sessions", query={"q": q})
        assert status == 200 and ids(page) == [kras], q
    assert call("GET", "/sessions", query={"q": "missing"})[1]["items"] == []
    # an empty / whitespace query is the plain list
    assert len(call("GET", "/sessions", query={"q": "   "})[1]["items"]) == 2


def test_search_follows_renames_and_pages_with_its_own_cursor():
    for i in range(4):
        create(title=f"match {i}")
        create(title=f"other {i}")
    _, first, _ = call("GET", "/sessions", query={"q": "match", "limit": "3"})
    assert len(first["items"]) == 3 and first["has_more"] is True
    _, rest, _ = call("GET", "/sessions", query={"q": "match", "limit": "3", "cursor": first["next_cursor"]})
    titles = [i["title"] for i in first["items"] + rest["items"]]
    assert sorted(titles) == [f"match {i}" for i in range(4)] and rest["has_more"] is False
    # a search cursor is not a list cursor (and vice versa)
    status, _, _ = call("GET", "/sessions", query={"limit": "3", "cursor": first["next_cursor"]})
    assert status == 400

    sid = first["items"][0]["session_id"]
    call("PATCH", "/sessions/{sessionId}", path={"sessionId": sid}, body={"title": "Pitch deck"})
    assert ids(call("GET", "/sessions", query={"q": "pitch"})[1]) == [sid]


def test_search_query_is_validated():
    assert call("GET", "/sessions", query={"q": "x" * 101})[0] == 400
    assert call("GET", "/sessions", query={"q": "bad\x00"})[0] == 400


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


def test_session_stamped_with_another_owner_is_refused_before_any_read_or_write():
    sid = create(sub="user-a", title="mine")["session_id"]
    # the history partition says user-b owns this id (e.g. a colliding / replayed id): ownership wins
    boto3.resource("dynamodb", region_name="us-east-1").Table("ChatHistoryStore").put_item(Item={
        "session_id": sid, "sequence_number": 1, "message_id": "b1", "role": "human", "user_id": "user-b",
        "content_blocks": [], "citations": [], "timestamp": "2026-01-01T00:00:00.000000Z",
    })
    assert call("GET", "/sessions/{sessionId}", path={"sessionId": sid})[0] == 404
    status, _, _ = call("PATCH", "/sessions/{sessionId}", path={"sessionId": sid}, body={"title": "hijack"})
    assert status == 404
    assert handler._repository().get("user-a", sid).title == "mine"  # the PATCH never ran


def test_owner_lookup_failure_is_a_generic_500_and_nothing_runs(monkeypatch):
    import logging

    from http_errors import JsonFormatter
    from shared.persistence.chat_history import ChatHistoryRepository

    sid = create(title="mine")["session_id"]

    def broken_owner_of(self, session_id):
        raise RuntimeError("ProvisionedThroughputExceededException on ChatHistoryStore")

    records: list[str] = []
    capture = logging.Handler()
    capture.emit = lambda record: records.append(JsonFormatter().format(record))  # what CloudWatch receives
    handler.logger.addHandler(capture)
    monkeypatch.setattr(ChatHistoryRepository, "owner_of", broken_owner_of)
    try:
        get_status, get_body, _ = call("GET", "/sessions/{sessionId}", path={"sessionId": sid})
        patch_status, patch_body, _ = call(
            "PATCH", "/sessions/{sessionId}", path={"sessionId": sid}, body={"title": "changed"}
        )
    finally:
        handler.logger.removeHandler(capture)
    for status, body in ((get_status, get_body), (patch_status, patch_body)):
        assert status == 500
        assert body["error"] == {
            "code": "internal_error", "message": GENERIC_500, "request_id": "req-1", "retriable": False
        }
    assert handler._repository().get("user-a", sid).title == "mine"
    logged = "\n".join(records)
    assert "session ownership check failed" in logged and "ProvisionedThroughputExceededException" in logged


def test_unexpected_errors_use_the_generic_message(monkeypatch):
    def broken_list(*_, **__):
        raise RuntimeError("boom: internal detail")

    monkeypatch.setattr(handler._repository(), "list_by_user", broken_list)
    status, body, _ = call("GET", "/sessions")
    assert status == 500 and body["error"]["message"] == GENERIC_500
    assert "boom" not in json.dumps(body)


def test_user_id_in_body_is_rejected_not_trusted():
    status, body, _ = call(
        "POST", "/sessions", body={"title": "t", "mode": "science", "user_id": "victim"}
    )
    assert status == 400 and body["error"]["code"] == "invalid_request"


def test_client_cannot_choose_the_session_id():
    status, _, _ = call("POST", "/sessions", body={"title": "t", "mode": "science", "session_id": "default"})
    assert status == 400


def test_cursor_round_trip():
    made = [create(title=f"c{i}")["session_id"] for i in range(3)]
    _, first, _ = call("GET", "/sessions", query={"limit": "2"})
    assert len(first["items"]) == 2 and first["has_more"] is True
    _, second, _ = call("GET", "/sessions", query={"limit": "2", "cursor": first["next_cursor"]})
    seen = ids(first) + ids(second)
    assert sorted(seen) == sorted(made) and len(set(seen)) == 3
    assert second["has_more"] is False and second["next_cursor"] is None


def test_tampered_cursor_is_rejected():
    create(sub="user-a")
    create(sub="user-b")
    _, page, _ = call("GET", "/sessions", sub="user-b", query={"limit": "1"})
    other = base64.urlsafe_b64encode(
        json.dumps(
            {"user_id": "user-b", "session_id": "x", "last_activity_at": "2026-01-01T00:00:00.000000Z"}
        ).encode()
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


@pytest.mark.parametrize("sid", ["bad id", "default", "s" * 33, "6f1c2d3e000040008000000000000001"])
def test_non_uuid_session_ids_are_invalid_request(sid):
    assert call("GET", "/sessions/{sessionId}", path={"sessionId": sid})[0] == 400
    assert call("PATCH", "/sessions/{sessionId}", path={"sessionId": sid}, body={"title": "x"})[0] == 400


def test_malformed_bodies_and_params():
    assert call("POST", "/sessions")[1]["error"]["code"] == "invalid_request"
    sid = create()["session_id"]
    assert call("PATCH", "/sessions/{sessionId}", path={"sessionId": sid}, body={})[0] == 400
    for limit in ("0", "101", "abc"):
        assert call("GET", "/sessions", query={"limit": limit})[0] == 400


# --- call-tracking coverage: ownership first, nothing else touches the session ---------------------------


@pytest.fixture
def tracked(monkeypatch):
    """Record (in order) every ownership check and metadata-repository read/write the handler performs."""
    from shared.persistence.chat_metadata import ChatMetadataRepository

    calls: list[tuple] = []
    real_verify = handler.verify_session_owner
    real_get, real_update = ChatMetadataRepository.get, ChatMetadataRepository.update

    def verify(history, user_id, session_id):
        calls.append(("verify_session_owner", user_id, session_id))
        return real_verify(history, user_id, session_id)

    def get(self, user_id, session_id):
        calls.append(("get", user_id, session_id))
        return real_get(self, user_id, session_id)

    def update(self, user_id, session_id, **kwargs):
        calls.append(("update", user_id, session_id))
        return real_update(self, user_id, session_id, **kwargs)

    monkeypatch.setattr(handler, "verify_session_owner", verify)
    monkeypatch.setattr(ChatMetadataRepository, "get", get)
    monkeypatch.setattr(ChatMetadataRepository, "update", update)
    return calls


@pytest.mark.parametrize("sid", ["session-1", "default"])
def test_non_uuid_session_id_is_400_with_no_dynamodb_call(tracked, sid):
    status, body, _ = call("GET", "/sessions/{sessionId}", path={"sessionId": sid})
    assert status == 400 and body["error"]["code"] == "invalid_request"
    status, body, _ = call("PATCH", "/sessions/{sessionId}", path={"sessionId": sid}, body={"title": "x"})
    assert status == 400 and body["error"]["code"] == "invalid_request"
    assert tracked == []


def test_ownership_check_error_is_generic_500_logged_and_repo_untouched(tracked, monkeypatch, caplog):
    import logging

    import http_errors
    from shared.persistence.ownership import OwnershipCheckError

    sid = str(uuid.uuid4())

    def failing(history, user_id, session_id):
        tracked.append(("verify_session_owner", user_id, session_id))
        raise OwnershipCheckError("boom-secret-cause")

    monkeypatch.setattr(handler, "verify_session_owner", failing)
    handler.logger.addHandler(caplog.handler)  # the handler logger does not propagate to root
    try:
        with caplog.at_level(logging.ERROR, logger=handler.logger.name):
            responses = [
                call("GET", "/sessions/{sessionId}", path={"sessionId": sid}),
                call("PATCH", "/sessions/{sessionId}", path={"sessionId": sid}, body={"title": "x"}),
            ]
    finally:
        handler.logger.removeHandler(caplog.handler)
    for status, body, raw in responses:
        assert status == 500
        assert body["error"]["code"] == "internal_error"
        assert body["error"]["message"] == http_errors.GENERIC_INTERNAL_ERROR
        assert "boom-secret-cause" not in raw["body"]
    failures = [r for r in caplog.records if "session ownership check failed" in r.getMessage()]
    assert len(failures) == 2
    assert all(r.levelno == logging.ERROR and r.exc_info and "boom-secret-cause" in str(r.exc_info[1])
               for r in failures)
    assert tracked == [("verify_session_owner", "user-a", sid)] * 2  # no get/update ever ran


def test_foreign_owner_is_404_and_repo_untouched(tracked, monkeypatch):
    from shared.persistence.chat_metadata import SessionNotFoundError

    sid = create(title="mine")["session_id"]

    def foreign(history, user_id, session_id):
        tracked.append(("verify_session_owner", user_id, session_id))
        raise SessionNotFoundError(session_id)

    monkeypatch.setattr(handler, "verify_session_owner", foreign)
    status, body, _ = call("GET", "/sessions/{sessionId}", path={"sessionId": sid})
    assert status == 404 and body["error"]["code"] == "not_found"
    status, body, _ = call("PATCH", "/sessions/{sessionId}", path={"sessionId": sid}, body={"title": "hijack"})
    assert status == 404 and body["error"]["code"] == "not_found"
    assert tracked == [("verify_session_owner", "user-a", sid)] * 2


def test_owned_get_checks_ownership_before_reading(tracked):
    sid = create()["session_id"]
    assert call("GET", "/sessions/{sessionId}", path={"sessionId": sid})[0] == 200
    assert call("PATCH", "/sessions/{sessionId}", path={"sessionId": sid}, body={"title": "y"})[0] == 200
    assert [c[0] for c in tracked] == ["verify_session_owner", "get", "verify_session_owner", "update"]


def test_post_returns_fresh_uuid4_never_default():
    first, second = create()["session_id"], create()["session_id"]
    for sid in (first, second):
        assert uuid.UUID(sid).version == 4 and str(uuid.UUID(sid)) == sid
        assert sid != "default"
    assert first != second


@pytest.fixture
def list_calls(monkeypatch):
    from types import SimpleNamespace

    from shared.persistence.chat_metadata import ChatMetadataRepository

    captured: list[dict] = []

    def list_by_user(self, user_id, **kwargs):
        captured.append({"user_id": user_id, **kwargs})
        return SimpleNamespace(items=[], next_key=None)

    monkeypatch.setattr(ChatMetadataRepository, "list_by_user", list_by_user)
    return captured


def test_list_defaults_to_limit_25_and_activity_sort(list_calls):
    from shared.persistence.chat_metadata import SortKey

    status, body, _ = call("GET", "/sessions")
    assert status == 200 and body["items"] == [] and body["has_more"] is False
    assert list_calls == [{"user_id": "user-a", "sort": SortKey.ACTIVITY, "limit": 25,
                           "start_key": None, "query": None}]


def test_list_search_query_is_normalized_before_the_repository(list_calls):
    from shared.persistence.chat_metadata import normalize_title

    assert call("GET", "/sessions", query={"q": "  Foo  "})[0] == 200
    assert list_calls[0]["query"] == normalize_title("  Foo  ") == "foo"


@pytest.mark.parametrize("query", [
    {"q": "x" * (handler.SEARCH_QUERY_MAX + 1)},
    {"q": "foo\x07bar"},
    {"q": "foo\nbar"},
    {"sort": "bogus"},
    {"cursor": base64.urlsafe_b64encode(json.dumps(
        {"user_id": "user-b", "session_id": "x", "last_activity_at": "2026-01-01T00:00:00.000000Z"}
    ).encode()).decode()},
])
def test_invalid_list_params_are_400_without_querying(list_calls, query):
    status, body, _ = call("GET", "/sessions", query=query)
    assert status == 400 and body["error"]["code"] == "invalid_request"
    assert list_calls == []


def test_search_query_at_max_length_is_accepted(list_calls):
    assert call("GET", "/sessions", query={"q": "x" * handler.SEARCH_QUERY_MAX})[0] == 200
    assert len(list_calls) == 1


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
