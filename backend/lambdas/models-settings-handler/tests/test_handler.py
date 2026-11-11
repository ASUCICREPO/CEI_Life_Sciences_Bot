import json
import sys
from pathlib import Path
from unittest.mock import Mock

import boto3
import pytest
from moto import mock_aws

LAMBDAS = Path(__file__).resolve().parents[2]
# ADR: tests import `shared.persistence`/`shared.runtime` from backend/agents (the layer ships a subset of it)
# and `http_errors` from lambdas/shared — same pattern as session-metadata-handler's tests.
sys.path.insert(0, str(LAMBDAS / "models-settings-handler"))
sys.path.insert(0, str(LAMBDAS / "shared"))
sys.path.insert(0, str(LAMBDAS.parent / "agents"))

import handler  # noqa: E402

ORIGIN = "https://site.example.net"
SONNET = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
HAIKU = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
BLOCKED = "anthropic.claude-fable-5-v1"


def _bedrock_stub(foundation_ids):
    client = Mock()
    client.list_foundation_models.return_value = {
        "modelSummaries": [
            {
                "modelId": mid,
                "modelName": mid,
                "outputModalities": ["TEXT"],
                "inputModalities": ["TEXT"],
                "responseStreamingSupported": True,
                "inferenceTypesSupported": ["ON_DEMAND"],
            }
            for mid in foundation_ids
        ]
    }
    client.list_inference_profiles.return_value = {"inferenceProfileSummaries": []}
    return client


@pytest.fixture(autouse=True)
def stubs(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("ALLOWED_ORIGINS", ORIGIN)
    from shared.runtime import model_discovery

    model_discovery._cache.clear()
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="UserMetadataTable",
            KeySchema=[
                {"AttributeName": "user_id", "KeyType": "HASH"},
                {"AttributeName": "record", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "user_id", "AttributeType": "S"},
                {"AttributeName": "record", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        handler._repo = None
        handler._bedrock_client = _bedrock_stub([SONNET, HAIKU, BLOCKED])
        yield


def call(method, resource, *, sub="user-a", body=None, claims=True):
    event = {
        "httpMethod": method,
        "resource": resource,
        "headers": {"Origin": ORIGIN},
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {"requestId": "req-1"},
    }
    if claims:
        event["requestContext"]["authorizer"] = {"claims": {"sub": sub}}
    response = handler.handler(event, None)
    return response["statusCode"], json.loads(response["body"]), response


def test_get_models_excludes_the_blocklist():
    status, body, _ = call("GET", "/models")
    assert status == 200
    assert body == {"models": [{"model_id": SONNET, "label": SONNET}, {"model_id": HAIKU, "label": HAIKU}]}


def test_get_settings_defaults_to_null_when_unset():
    status, body, _ = call("GET", "/settings")
    assert status == 200
    assert body == {"default_model_id": None, "default_mode": None, "configured_sources": []}


def test_put_settings_round_trips():
    status, body, _ = call("PUT", "/settings", body={"default_model_id": HAIKU, "default_mode": "business"})
    assert status == 200
    assert body == {"default_model_id": HAIKU, "default_mode": "business", "configured_sources": []}
    status, body, _ = call("GET", "/settings")
    assert status == 200 and body["default_model_id"] == HAIKU and body["default_mode"] == "business"


def test_put_settings_rejects_blocklisted_model():
    status, body, _ = call("PUT", "/settings", body={"default_model_id": BLOCKED, "default_mode": "science"})
    assert status == 400 and body["error"]["code"] == "invalid_request"


def test_put_settings_rejects_undiscovered_model():
    status, body, _ = call("PUT", "/settings", body={"default_model_id": "unknown.model-v1"})
    assert status == 400 and body["error"]["code"] == "invalid_request"


def test_put_settings_rejects_invalid_mode():
    status, body, _ = call("PUT", "/settings", body={"default_mode": "admin"})
    assert status == 400 and body["error"]["code"] == "invalid_request"


def test_put_settings_rejects_unknown_field():
    status, body, _ = call("PUT", "/settings", body={"default_mode": "science", "extra": 1})
    assert status == 400 and body["error"]["code"] == "invalid_request"


def test_settings_are_isolated_per_user():
    call("PUT", "/settings", sub="user-a", body={"default_model_id": SONNET})
    status, body, _ = call("GET", "/settings", sub="user-b")
    assert status == 200 and body["default_model_id"] is None


def test_missing_claims_is_unauthorized():
    status, body, _ = call("GET", "/settings", claims=False)
    assert status == 401 and body["error"]["code"] == "unauthorized"


def test_unknown_route_is_not_found():
    status, body, _ = call("DELETE", "/settings")
    assert status == 404 and body["error"]["code"] == "not_found"
