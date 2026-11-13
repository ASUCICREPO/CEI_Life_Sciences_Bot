import base64
import json
import os
import sys
import uuid
from pathlib import Path

LAMBDAS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(LAMBDAS / "attachment-presign"))

# Module-level constants in handler.py are read from the environment at import time.
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ["ATTACHMENTS_TABLE"] = "attachments"
os.environ["ATTACHMENTS_BUCKET"] = "cei-attachments"

import boto3  # noqa: E402
import pytest  # noqa: E402
from moto import mock_aws  # noqa: E402

import handler  # noqa: E402

USER_ID = "user-abc"
SESSION_ID = str(uuid.uuid4())


@pytest.fixture
def aws():
    with mock_aws():
        handler.s3 = boto3.client("s3", region_name="us-east-1")
        handler.dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        handler.s3.create_bucket(Bucket="cei-attachments")
        handler.dynamodb.create_table(
            TableName="attachments",
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}, {"AttributeName": "attachment_id", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"}, {"AttributeName": "attachment_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield


def _event(body: dict, sub: str | None = USER_ID) -> dict:
    claims = {"sub": sub} if sub else {}
    return {"httpMethod": "POST", "requestContext": {"authorizer": {"claims": claims}}, "body": json.dumps(body)}


def _body(**extra) -> dict:
    return {"filename": "report.pdf", "content_type": "application/pdf", "session_id": SESSION_ID, **extra}


def _policy(fields: dict) -> dict:
    return json.loads(base64.b64decode(fields["policy"]))


def test_returns_presigned_post_with_s3_enforced_size_cap(aws):
    resp = handler.handler(_event(_body(size=1024)), None)
    assert resp["statusCode"] == 200
    data = json.loads(resp["body"])
    assert data["upload_url"].startswith("https://cei-attachments.")
    assert data["max_bytes"] == 55_000_000
    fields = data["upload_fields"]
    assert fields["key"] == data["s3_key"]
    assert fields["Content-Type"] == "application/pdf"
    conditions = _policy(fields)["conditions"]
    assert ["content-length-range", 1, 55_000_000] in conditions
    assert {"Content-Type": "application/pdf"} in conditions
    assert {"key": data["s3_key"]} in conditions


def test_key_is_scoped_to_caller_and_session(aws):
    data = json.loads(handler.handler(_event(_body()), None)["body"])
    assert data["s3_key"] == f"uploads/{USER_ID}/{SESSION_ID}/{data['attachment_id']}/report.pdf"


def test_writes_pending_metadata_row(aws):
    data = json.loads(handler.handler(_event(_body()), None)["body"])
    item = handler.dynamodb.Table("attachments").get_item(
        Key={"user_id": USER_ID, "attachment_id": data["attachment_id"]})["Item"]
    assert item["status"] == "pending_upload"
    assert item["session_id"] == SESSION_ID


def test_size_is_optional(aws):
    assert handler.handler(_event(_body()), None)["statusCode"] == 200


@pytest.mark.parametrize("size", [55_000_001, 0, -1, "10", True, 1.5])
def test_rejects_bad_or_oversize_declared_size(aws, size):
    resp = handler.handler(_event(_body(size=size)), None)
    assert resp["statusCode"] == 400


def test_accepts_size_at_cap(aws):
    assert handler.handler(_event(_body(size=55_000_000)), None)["statusCode"] == 200


def test_rejects_unsupported_type(aws):
    assert handler.handler(_event(_body(content_type="image/png")), None)["statusCode"] == 400


def test_requires_auth(aws):
    assert handler.handler(_event(_body(), sub=None), None)["statusCode"] == 401
