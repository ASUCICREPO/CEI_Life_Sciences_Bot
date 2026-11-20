import json
import sys
from pathlib import Path
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

LAMBDAS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(LAMBDAS / "jobs-handler"))
sys.path.insert(0, str(LAMBDAS / "shared"))
sys.path.insert(0, str(LAMBDAS.parent / "agents"))

import handler  # noqa: E402
from shared.persistence import jobs as job_store  # noqa: E402
from shared.persistence.jobs import JobRepository, new_job_id  # noqa: E402


@pytest.fixture(autouse=True)
def stores(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("CEI_JOB_TABLE", "jobs")
    monkeypatch.setenv("CEI_ACTIVE_RUNS_TABLE", "runs")
    monkeypatch.setenv("CEI_RUNTIME_STOP_URLS", json.dumps({"pipeline_manager": "https://agentcore.test/stop"}))
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="jobs", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}, {"AttributeName": "job_id", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": n, "AttributeType": "S"}
                                  for n in ("user_id", "job_id", "status", "updated_at")],
            GlobalSecondaryIndexes=[{"IndexName": job_store.STATUS_INDEX, "Projection": {"ProjectionType": "ALL"},
                                     "KeySchema": [{"AttributeName": "status", "KeyType": "HASH"},
                                                   {"AttributeName": "updated_at", "KeyType": "RANGE"}]}])
        ddb.create_table(
            TableName="runs", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}, {"AttributeName": "record", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"},
                                  {"AttributeName": "record", "AttributeType": "S"}])
        handler._jobs = None
        handler._runs = None
        yield JobRepository("jobs", dynamodb=ddb)


def event(method, resource, *, user="alice", job_id=None, query=None, token="Bearer user-token", extra_claims=None):
    return {"httpMethod": method, "resource": resource, "headers": {"Authorization": token},
            "pathParameters": {"jobId": job_id} if job_id else None, "queryStringParameters": query,
            # B112: every call defaults to an age-verified token; tests of that gate override extra_claims.
            "requestContext": {"requestId": "r1", "authorizer": {
                "claims": {"sub": user, "cei_age_verified": "true", **(extra_claims or {})}}}}


def make_job(repo, user="alice", **kwargs):
    job = repo.create(user_id=user, session_id="chat-1", agent_type="pipeline_manager",
                      runtime_session_id="rt-session", steps=[("s1", "research", "Find")], **kwargs)
    repo.mark_running(user, job["job_id"])
    return job


def body(response):
    return json.loads(response["body"])


def test_list_returns_only_callers_jobs_newest_first(stores):
    old = make_job(stores, job_id=new_job_id(1000))
    new = make_job(stores, job_id=new_job_id(2000))
    make_job(stores, user="bob")
    result = body(handler.handler(event("GET", "/jobs"), None))
    assert [item["job_id"] for item in result["items"]] == [new["job_id"], old["job_id"]]
    assert all("runtime_session_id" not in item for item in result["items"])


def test_cursor_from_another_user_is_rejected(stores):
    from http_errors import encode_cursor
    cursor = encode_cursor({"user_id": "bob", "job_id": new_job_id(1)})
    response = handler.handler(event("GET", "/jobs", query={"cursor": cursor}), None)
    assert response["statusCode"] == 400


def test_status_is_owner_scoped_and_not_cached(stores):
    job = make_job(stores)
    ok = handler.handler(event("GET", "/jobs/{jobId}", job_id=job["job_id"]), None)
    assert ok["statusCode"] == 200 and body(ok)["status"] == "running"
    assert ok["headers"]["Cache-Control"] == "no-store"
    assert handler.handler(event("GET", "/jobs/{jobId}", user="bob", job_id=job["job_id"]), None)["statusCode"] == 404
    assert handler.handler(event("GET", "/jobs/{jobId}", job_id="../x"), None)["statusCode"] == 400


def test_cancel_stops_session_finalizes_and_frees_slot(stores):
    job = make_job(stores)
    handler._repositories()[1].acquire_background("alice", job["job_id"])
    with patch.object(handler, "_stop_runtime_session", return_value=True) as stop:
        response = handler.handler(event("POST", "/jobs/{jobId}/cancel", job_id=job["job_id"]), None)
    assert response["statusCode"] == 202 and body(response)["status"] == "cancelling"
    stop.assert_called_once_with("pipeline_manager", "rt-session", "Bearer user-token")
    assert stores.get("alice", job["job_id"])["status"] == "cancelled"
    assert handler._repositories()[1].background_holder("alice") is None


def test_cancel_still_wins_when_runtime_stop_fails(stores):
    job = make_job(stores)
    with patch.object(handler, "_stop_runtime_session", return_value=False):
        handler.handler(event("POST", "/jobs/{jobId}/cancel", job_id=job["job_id"]), None)
    assert stores.get("alice", job["job_id"])["status"] == "cancelled"


def test_cancel_of_finished_job_is_idempotent(stores):
    job = make_job(stores)
    stores.finish("alice", job["job_id"], "completed")
    with patch.object(handler, "_stop_runtime_session") as stop:
        response = handler.handler(event("POST", "/jobs/{jobId}/cancel", job_id=job["job_id"]), None)
    assert response["statusCode"] == 200 and body(response)["status"] == "completed"
    stop.assert_not_called()


def test_cancel_is_blocked_without_the_age_verified_claim(stores):
    job = make_job(stores)
    with patch.object(handler, "_stop_runtime_session") as stop:
        response = handler.handler(
            event("POST", "/jobs/{jobId}/cancel", job_id=job["job_id"], extra_claims={"cei_age_verified": "false"}),
            None,
        )
    assert response["statusCode"] == 403 and body(response)["error"]["code"] == "onboarding_required"
    stop.assert_not_called()
    assert stores.get("alice", job["job_id"])["status"] == "running"
    # GET /jobs is a read, not a run-starting write, and stays unaffected by the gate.
    read = handler.handler(event("GET", "/jobs", extra_claims={"cei_age_verified": "false"}), None)
    assert read["statusCode"] == 200


def test_other_user_cannot_cancel(stores):
    job = make_job(stores)
    with patch.object(handler, "_stop_runtime_session") as stop:
        response = handler.handler(event("POST", "/jobs/{jobId}/cancel", user="bob", job_id=job["job_id"]), None)
    assert response["statusCode"] == 404
    stop.assert_not_called()
    assert stores.get("alice", job["job_id"])["status"] == "running"


def test_stop_request_uses_runtime_session_and_callers_token():
    captured = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def fake_urlopen(request, timeout):
        captured.update(url=request.full_url, headers=dict(request.header_items()), timeout=timeout)
        return Response()

    with patch.object(handler.urllib.request, "urlopen", fake_urlopen):
        assert handler._stop_runtime_session("pipeline_manager", "rt-session", "Bearer t") is True
    assert captured["url"] == "https://agentcore.test/stop"
    assert captured["headers"]["Authorization"] == "Bearer t"
    assert captured["headers"]["X-amzn-bedrock-agentcore-runtime-session-id"] == "rt-session"
    assert handler._stop_runtime_session("unknown", "rt", "Bearer t") is False
    assert handler._stop_runtime_session("pipeline_manager", "rt", None) is False


def test_unauthenticated_and_unknown_routes():
    no_claims = {"httpMethod": "GET", "resource": "/jobs", "requestContext": {}}
    assert handler.handler(no_claims, None)["statusCode"] == 401
    assert handler.handler(event("DELETE", "/jobs"), None)["statusCode"] == 404
