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
# Session ids are dedicated UUIDs; anything else is rejected before the session is touched
S1 = "6f1c2d3e-0000-4000-8000-000000000001"
D1 = "6f1c2d3e-0000-4000-8000-000000000002"
EMPTY = "6f1c2d3e-0000-4000-8000-000000000003"
GENERIC_500 = "It's not you, it's us. Something went wrong"


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
                "session_id": S1,
                "title": "t",
                "status": "active",
                "message_count": 0,
                "agent_type": "analysis",
                "mode": "business",
                "created_at": NOW.isoformat(),
                "updated_at": NOW.isoformat(),
            }
        )
        for seq in range(1, 6):
            history.put_item(
                Item={
                    "session_id": S1,
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
            Item={"session_id": S1, "sequence_number": 6, "record_type": "trace_event"}
        )
        handler._metadata = None
        handler._history = None
        yield


def call(sid=S1, *, sub="user-a", query=None, claims=True):
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
    assert set(item) == {
        "message_id", "role", "content_blocks", "citations", "timestamp", "sequence_number", "truncated"
    }


def test_envelope_carries_session_metadata_for_reopening_the_chat():
    _, body = call()
    assert body["session"] == {
        "session_id": S1, "title": "t", "title_source": "prompt",
        "agent_type": "analysis", "mode": "business", "model_id": None,
        "document_type": None, "analysis_type": None,
    }


def test_envelope_carries_stored_turn_context():
    boto3.resource("dynamodb", region_name="us-east-1").Table("ChatMetadataStore").put_item(
        Item={
            "user_id": "user-a",
            "session_id": D1,
            "title": "t",
            "status": "active",
            "message_count": 0,
            "agent_type": "drafting",
            "document_type": "grant",
            "analysis_type": "competitor",
            "created_at": NOW.isoformat(),
            "updated_at": NOW.isoformat(),
        }
    )
    _, body = call(D1)
    assert body["session"]["agent_type"] == "drafting"
    assert body["session"]["document_type"] == "grant"
    assert body["session"]["analysis_type"] == "competitor"
    assert "user_id" not in body["session"]


def test_default_page_size_is_20():
    table = boto3.resource("dynamodb", region_name="us-east-1").Table("ChatHistoryStore")
    for seq in range(7, 40):
        table.put_item(Item={"session_id": S1, "sequence_number": seq, "message_id": f"m{seq}", "role": "ai",
                             "user_id": "user-a", "content_blocks": [], "citations": [],
                             "timestamp": NOW.isoformat()})
    _, body = call()
    assert len(body["items"]) == 20 and body["items"][-1]["sequence_number"] == 39


def test_items_stamped_with_another_owner_are_never_returned():
    table = boto3.resource("dynamodb", region_name="us-east-1").Table("ChatHistoryStore")
    table.put_item(Item={"session_id": S1, "sequence_number": 7, "message_id": "x", "role": "human",
                         "user_id": "user-b", "content_blocks": [{"block_type": "text", "body": "foreign"}],
                         "citations": [], "timestamp": NOW.isoformat()})
    _, body = call(query={"limit": "100"})
    assert "x" not in [i["message_id"] for i in body["items"]]
    assert all("user_id" not in i for i in body["items"])


def test_session_stamped_with_another_owner_is_not_found_even_with_own_metadata_row():
    # user-a has a metadata row for D1, but the history partition is stamped with user-b: ownership wins
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.Table("ChatMetadataStore").put_item(Item={
        "user_id": "user-a", "session_id": D1, "title": "t", "status": "active", "message_count": 0,
        "created_at": NOW.isoformat(), "updated_at": NOW.isoformat(),
    })
    ddb.Table("ChatHistoryStore").put_item(Item={
        "session_id": D1, "sequence_number": 1, "message_id": "b1", "role": "human", "user_id": "user-b",
        "content_blocks": [{"block_type": "text", "body": "b's prompt"}], "citations": [],
        "timestamp": NOW.isoformat(),
    })
    status, body = call(D1)
    assert status == 404 and body["error"]["code"] == "not_found"


def test_owner_lookup_failure_is_a_generic_500_and_the_cause_is_logged(monkeypatch):
    import logging

    from http_errors import JsonFormatter
    from shared.persistence.chat_history import ChatHistoryRepository

    def broken_owner_of(self, session_id):
        raise RuntimeError("AccessDeniedException: dynamodb:Query on ChatHistoryStore")

    records: list[str] = []
    capture = logging.Handler()
    capture.emit = lambda record: records.append(JsonFormatter().format(record))  # what CloudWatch receives
    handler.logger.addHandler(capture)
    monkeypatch.setattr(ChatHistoryRepository, "owner_of", broken_owner_of)
    try:
        status, body = call()
    finally:
        handler.logger.removeHandler(capture)
    assert status == 500
    assert body["error"]["code"] == "internal_error" and body["error"]["message"] == GENERIC_500
    assert "AccessDenied" not in json.dumps(body)  # never leaked to the caller
    logged = "\n".join(records)
    assert "session ownership check failed" in logged
    assert '"exception_type": "OwnershipCheckError"' in logged
    assert '"cause_type": "RuntimeError"' in logged
    assert "AccessDeniedException" not in logged


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
            "session_id": EMPTY,
            "title": "t",
            "status": "active",
            "message_count": 0,
            "created_at": NOW.isoformat(),
            "updated_at": NOW.isoformat(),
        }
    )
    status, body = call(EMPTY)
    assert status == 200
    assert {k: body[k] for k in ("items", "next_cursor", "has_more")} == {
        "items": [], "next_cursor": None, "has_more": False
    }
    assert body["session"]["session_id"] == EMPTY


def test_other_users_session_is_not_found():
    status, body = call(sub="user-b")
    assert status == 404 and body["error"]["code"] == "not_found"


def test_tampered_cursor_rejected():
    foreign = base64.urlsafe_b64encode(
        json.dumps({"session_id": "other-session", "sequence_number": 3}).encode()
    ).decode()
    # negative sort keys are reserved for checkpoint items, never a page cursor
    negative = base64.urlsafe_b64encode(json.dumps({"session_id": S1, "sequence_number": -1}).encode()).decode()
    for cursor in ("@@@", foreign, "e30", negative):
        status, body = call(query={"cursor": cursor})
        assert status == 400 and body["error"]["code"] == "invalid_request"


@pytest.mark.parametrize("sid", ["bad id", "default", "s1", "s" * 33, S1.replace("-", "")])
def test_non_uuid_session_ids_are_invalid_request(sid):
    assert call(sid)[1]["error"]["code"] == "invalid_request"


# --- call-tracking coverage: ownership first; metadata GetItem and history page load only after it -------


@pytest.fixture
def tracked(monkeypatch):
    from shared.persistence.chat_history import ChatHistoryRepository
    from shared.persistence.chat_metadata import ChatMetadataRepository

    calls: list[str] = []
    real_verify = handler.verify_session_owner
    real_get, real_load = ChatMetadataRepository.get, ChatHistoryRepository.load_page

    def verify(history, user_id, session_id):
        calls.append("verify_session_owner")
        return real_verify(history, user_id, session_id)

    def get(self, *args, **kwargs):
        calls.append("metadata.get")
        return real_get(self, *args, **kwargs)

    def load_page(self, *args, **kwargs):
        calls.append("history.load_page")
        return real_load(self, *args, **kwargs)

    monkeypatch.setattr(handler, "verify_session_owner", verify)
    monkeypatch.setattr(ChatMetadataRepository, "get", get)
    monkeypatch.setattr(ChatHistoryRepository, "load_page", load_page)
    return calls


def test_owned_session_checks_ownership_before_metadata_and_history(tracked):
    assert call()[0] == 200
    assert tracked == ["verify_session_owner", "metadata.get", "history.load_page"]


def test_foreign_owner_stops_before_metadata_get_and_history_load(tracked, monkeypatch):
    from shared.persistence.chat_metadata import SessionNotFoundError

    def foreign(history, user_id, session_id):
        tracked.append("verify_session_owner")
        raise SessionNotFoundError(session_id)

    monkeypatch.setattr(handler, "verify_session_owner", foreign)
    status, body = call()
    assert status == 404 and body["error"]["code"] == "not_found"
    assert tracked == ["verify_session_owner"]


def test_foreign_stamped_owner_in_store_stops_before_metadata_get_and_history_load(tracked):
    # the owner is the stamp on the partition's FIRST item; user-a still has its own metadata row for D1
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.Table("ChatMetadataStore").put_item(Item={
        "user_id": "user-a", "session_id": D1, "title": "t", "status": "active", "message_count": 0,
        "created_at": NOW.isoformat(), "updated_at": NOW.isoformat(),
    })
    ddb.Table("ChatHistoryStore").put_item(Item={"session_id": D1, "sequence_number": 1, "message_id": "b",
                                                 "role": "human", "user_id": "user-b", "content_blocks": [],
                                                 "citations": [], "timestamp": NOW.isoformat()})
    assert call(D1)[0] == 404
    assert tracked == ["verify_session_owner"]


def test_ownership_check_error_is_generic_500_before_any_session_read(tracked, monkeypatch, caplog):
    import logging

    import http_errors
    from shared.persistence.ownership import OwnershipCheckError

    def failing(history, user_id, session_id):
        tracked.append("verify_session_owner")
        raise OwnershipCheckError("boom-secret-cause")

    monkeypatch.setattr(handler, "verify_session_owner", failing)
    handler.logger.addHandler(caplog.handler)  # the handler logger does not propagate to root
    try:
        event = {
            "httpMethod": "GET", "resource": "/sessions/{sessionId}/history",
            "pathParameters": {"sessionId": S1}, "queryStringParameters": None,
            "requestContext": {"requestId": "req-1", "authorizer": {"claims": {"sub": "user-a"}}},
        }
        raw = handler.handler(event, None)
    finally:
        handler.logger.removeHandler(caplog.handler)
    body = json.loads(raw["body"])
    assert raw["statusCode"] == 500
    assert body["error"]["code"] == "internal_error"
    assert body["error"]["message"] == http_errors.GENERIC_INTERNAL_ERROR
    assert "boom-secret-cause" not in raw["body"]
    failures = [r for r in caplog.records if "session ownership check failed" in r.getMessage()]
    assert len(failures) == 1 and failures[0].levelno == logging.ERROR
    assert failures[0].exc_info and "boom-secret-cause" in str(failures[0].exc_info[1])
    assert tracked == ["verify_session_owner"]


@pytest.mark.parametrize("sid", ["session-1", "default"])
def test_non_uuid_session_id_is_400_with_no_store_calls(tracked, sid):
    status, body = call(sid)
    assert status == 400 and body["error"]["code"] == "invalid_request"
    assert tracked == []


def test_malformed_input_is_invalid_request():
    assert call(query={"limit": "101"})[1]["error"]["code"] == "invalid_request"


def test_missing_claims_is_unauthorized():
    status, body = call(claims=False)
    assert status == 401 and body["error"]["code"] == "unauthorized"


# ---- GET /sessions/{sessionId}/turns/{messageId}/trace (F6, B59) --------------------------------------------------

TRACE_EVENTS = [
    {"seq": 0, "type": "trace_event", "step_type": "tool_call", "tool_name": "pubmed___search",
     "tool_output_summary": "12 results retrieved"},
    {"seq": 1, "type": "trace_event", "step_type": "reasoning", "reasoning_text": "done",
     "reasoning_label": "summarized"},
]


def _seed_trace(owner="user-a", message_id="m3", turn_seq=3, events=TRACE_EVENTS):
    from shared.persistence.traces import TraceRepository

    table = boto3.resource("dynamodb", region_name="us-east-1").Table("ChatHistoryStore")
    assert TraceRepository(table=table, redactor=dict).save(owner, S1, turn_seq, message_id, events)


def call_trace(sid=S1, message_id="m3", *, sub="user-a"):
    handler._traces = None
    event = {
        "httpMethod": "GET",
        "resource": "/sessions/{sessionId}/turns/{messageId}/trace",
        "pathParameters": {"sessionId": sid, "messageId": message_id},
        "queryStringParameters": None,
        "requestContext": {"requestId": "req-1", "authorizer": {"claims": {"sub": sub}}},
    }
    response = handler.handler(event, None)
    return response, json.loads(response["body"])


def test_trace_route_returns_events_with_private_cache():
    _seed_trace()
    response, body = call_trace()
    assert response["statusCode"] == 200
    assert body == {"message_id": "m3", "events": TRACE_EVENTS}
    assert response["headers"]["Cache-Control"] == "private, max-age=300"


def test_trace_route_missing_trace_or_unknown_message_is_404():
    assert call_trace(message_id="m3")[0]["statusCode"] == 404
    assert call_trace(message_id="nope")[0]["statusCode"] == 404


def test_trace_route_foreign_trace_head_is_404():
    _seed_trace(owner="user-b")
    response, body = call_trace()
    assert response["statusCode"] == 404 and body["error"]["code"] == "not_found"


def test_trace_route_other_users_session_is_404():
    _seed_trace()
    assert call_trace(sub="user-b")[0]["statusCode"] == 404


@pytest.mark.parametrize("message_id", ["../x", "a b", "x" * 129, ""])
def test_trace_route_invalid_message_id_is_400_with_no_store_calls(tracked, message_id):
    response, body = call_trace(message_id=message_id)
    assert response["statusCode"] == 400 and body["error"]["code"] == "invalid_request"
    assert tracked == []


def test_trace_route_checks_ownership_before_metadata_and_trace(tracked, monkeypatch):
    from shared.persistence.traces import TraceRepository

    real_load = TraceRepository.load

    def load(self, *args, **kwargs):
        tracked.append("traces.load")
        return real_load(self, *args, **kwargs)

    monkeypatch.setattr(TraceRepository, "load", load)
    _seed_trace()
    assert call_trace()[0]["statusCode"] == 200
    assert tracked == ["verify_session_owner", "metadata.get", "traces.load"]


def test_history_never_includes_trace_items():
    _seed_trace()
    _, body = call()
    assert [i["sequence_number"] for i in body["items"]] == [1, 2, 3, 4, 5]
    assert all("events" not in i for i in body["items"])


def test_unknown_route_is_404():
    event = {
        "httpMethod": "POST", "resource": "/sessions/{sessionId}/turns/{messageId}/trace",
        "pathParameters": {"sessionId": S1, "messageId": "m3"},
        "requestContext": {"requestId": "req-1", "authorizer": {"claims": {"sub": "user-a"}}},
    }
    assert handler.handler(event, None)["statusCode"] == 404
