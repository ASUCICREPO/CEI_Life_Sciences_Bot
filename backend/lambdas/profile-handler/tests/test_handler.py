import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

LAMBDAS = Path(__file__).resolve().parents[2]
# ADR: same import layout as the other REST Lambdas' tests (models-settings-handler) -- `http_errors` from
# lambdas/shared (the deployed layer ships it flat), nothing from backend/agents needed here.
sys.path.insert(0, str(LAMBDAS / "profile-handler"))
sys.path.insert(0, str(LAMBDAS / "shared"))

import handler  # noqa: E402

TODAY = datetime.now(UTC).date()
ADULT_BIRTHDATE = TODAY.replace(year=TODAY.year - 25).isoformat()
MINOR_BIRTHDATE = TODAY.replace(year=TODAY.year - 10).isoformat()
USER_POOL_ID_ENV = "us-east-1_test00000"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("CEI_AWS_REGION", "us-east-1")
    monkeypatch.setenv("USER_POOL_ID", USER_POOL_ID_ENV)
    handler._cognito_client = None
    with mock_aws():
        cognito = boto3.client("cognito-idp", region_name="us-east-1")
        pool = cognito.create_user_pool(PoolName="cei-test-pool")
        pool_id = pool["UserPool"]["Id"]
        monkeypatch.setenv("USER_POOL_ID", pool_id)
        user = cognito.admin_create_user(UserPoolId=pool_id, Username="someone@example.com")
        sub = {a["Name"]: a["Value"] for a in user["User"]["Attributes"]}["sub"]
        yield {"pool_id": pool_id, "username": user["User"]["Username"], "sub": sub}


def _event(sub: str, body: dict | None) -> dict:
    return {
        "httpMethod": "POST",
        "resource": "/profile",
        "requestContext": {"requestId": "req-1", "authorizer": {"claims": {"sub": sub}}},
        "body": json.dumps(body) if body is not None else None,
    }


def _call(env, body, sub=None):
    response = handler.handler(_event(sub or env["sub"], body), None)
    return response["statusCode"], json.loads(response["body"])


def test_creates_profile_and_returns_it(env):
    status, body = _call(env, {"given_name": "Ada", "family_name": "Lovelace", "birthdate": ADULT_BIRTHDATE})
    assert status == 200
    assert body == {
        "status": "ok",
        "profile": {"given_name": "Ada", "family_name": "Lovelace", "birthdate": ADULT_BIRTHDATE},
    }
    cognito = boto3.client("cognito-idp", region_name="us-east-1")
    attrs = {
        a["Name"]: a["Value"]
        for a in cognito.admin_get_user(UserPoolId=env["pool_id"], Username=env["username"])["UserAttributes"]
    }
    assert attrs["given_name"] == "Ada"
    assert attrs["family_name"] == "Lovelace"
    assert attrs["birthdate"] == ADULT_BIRTHDATE
    # Still enabled: an adult's account is untouched.
    user = cognito.admin_get_user(UserPoolId=env["pool_id"], Username=env["username"])
    assert user["Enabled"] is True


def test_under_18_is_disabled_and_signed_out_after_the_attributes_are_written(env):
    status, body = _call(env, {"given_name": "Pat", "family_name": "Young", "birthdate": MINOR_BIRTHDATE})
    assert status == 403
    assert body["error"]["code"] == "age_restricted"
    cognito = boto3.client("cognito-idp", region_name="us-east-1")
    user = cognito.admin_get_user(UserPoolId=env["pool_id"], Username=env["username"])
    assert user["Enabled"] is False
    attrs = {a["Name"]: a["Value"] for a in user["UserAttributes"]}
    # The attributes are stored anyway (B112), even though the account is disabled.
    assert attrs["given_name"] == "Pat"
    assert attrs["birthdate"] == MINOR_BIRTHDATE


@pytest.mark.parametrize(
    "birthdate,expect_adult",
    [
        (lambda today: today.replace(year=today.year - 18).isoformat(), True),
        (lambda today: today.replace(year=today.year - 17).isoformat(), False),
    ],
)
def test_age_boundary_is_exactly_18_today(env, birthdate, expect_adult):
    status, body = _call(env, {"given_name": "Sam", "family_name": "Lee", "birthdate": birthdate(TODAY)})
    if expect_adult:
        assert status == 200
    else:
        assert status == 403 and body["error"]["code"] == "age_restricted"


def test_second_submission_is_rejected_as_profile_exists(env):
    _call(env, {"given_name": "Ada", "family_name": "Lovelace", "birthdate": ADULT_BIRTHDATE})
    status, body = _call(env, {"given_name": "Ada", "family_name": "Lovelace", "birthdate": ADULT_BIRTHDATE})
    assert status == 409 and body["error"]["code"] == "profile_exists"


def test_unknown_field_is_rejected(env):
    status, body = _call(env, {"given_name": "Ada", "family_name": "Lovelace", "birthdate": ADULT_BIRTHDATE, "extra": 1})
    assert status == 400 and body["error"]["code"] == "invalid_request"


@pytest.mark.parametrize("missing", ["given_name", "family_name", "birthdate"])
def test_missing_field_is_rejected(env, missing):
    body = {"given_name": "Ada", "family_name": "Lovelace", "birthdate": ADULT_BIRTHDATE}
    del body[missing]
    status, response_body = _call(env, body)
    assert status == 400 and response_body["error"]["code"] == "invalid_request"


@pytest.mark.parametrize(
    "value",
    ["", " ", "x" * 51, "Ada123", "Ada<script>", "Ada\nLovelace"],
)
def test_invalid_names_are_rejected(env, value):
    status, body = _call(env, {"given_name": value, "family_name": "Lovelace", "birthdate": ADULT_BIRTHDATE})
    assert status == 400 and body["error"]["code"] == "invalid_request"


@pytest.mark.parametrize("value", ["Mary Jane", "O'Brien", "Smith-Jones", "St. Clair", "Renée", "Renée", "Zoë", "李"])
def test_valid_name_punctuation_and_unicode_are_accepted(env, value):
    status, _ = _call(env, {"given_name": value, "family_name": "Lovelace", "birthdate": ADULT_BIRTHDATE})
    assert status == 200


@pytest.mark.parametrize(
    "birthdate",
    ["not-a-date", "2020/01/01", "2020-02-30", "1899-01-01", "2999-01-01", 20200101, None],
)
def test_invalid_birthdates_are_rejected(env, birthdate):
    status, body = _call(env, {"given_name": "Ada", "family_name": "Lovelace", "birthdate": birthdate})
    assert status == 400 and body["error"]["code"] == "invalid_request"


def test_future_birthdate_is_rejected(env):
    future = TODAY.replace(year=TODAY.year + 1).isoformat()
    status, body = _call(env, {"given_name": "Ada", "family_name": "Lovelace", "birthdate": future})
    assert status == 400 and body["error"]["code"] == "invalid_request"


def test_missing_claims_is_unauthorized(env):
    event = {"httpMethod": "POST", "resource": "/profile", "requestContext": {"requestId": "req-1"},
              "body": json.dumps({"given_name": "Ada", "family_name": "Lovelace", "birthdate": ADULT_BIRTHDATE})}
    response = handler.handler(event, None)
    assert response["statusCode"] == 401


def test_unknown_route_is_not_found(env):
    event = _event(env["sub"], None)
    event["httpMethod"] = "GET"
    response = handler.handler(event, None)
    assert response["statusCode"] == 404


def test_logs_never_contain_the_name_or_birthdate(env, caplog):
    caplog.set_level("INFO")
    _call(env, {"given_name": "Ada", "family_name": "Lovelace", "birthdate": ADULT_BIRTHDATE})
    assert "Ada" not in caplog.text and "Lovelace" not in caplog.text and ADULT_BIRTHDATE not in caplog.text
