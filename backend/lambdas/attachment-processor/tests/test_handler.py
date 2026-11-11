import json
import os
import sys
import uuid
from collections import deque
from pathlib import Path

LAMBDAS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(LAMBDAS / "attachment-processor"))

# Module-level constants in handler.py are read from the environment at import time.
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ["ATTACHMENTS_TABLE"] = "attachments"
os.environ["ATTACHMENTS_BUCKET"] = "cei-attachments"
os.environ["KNOWLEDGE_BASE_ID"] = "kb-123"
os.environ["KNOWLEDGE_BASE_DATA_SOURCE_ID"] = "ds-456"

import boto3  # noqa: E402
import pytest  # noqa: E402
from moto import mock_aws  # noqa: E402

import handler  # noqa: E402

USER_ID = "user-abc"
SESSION_ID = str(uuid.uuid4())
ATTACHMENT_ID = str(uuid.uuid4())
FILENAME = "report.pdf"
KEY = f"uploads/{USER_ID}/{SESSION_ID}/{ATTACHMENT_ID}/{FILENAME}"
BUCKET = "cei-attachments"


class FakeBedrockAgent:
    """Minimal stand-in for the bedrock-agent client used by the processor."""

    def __init__(self):
        self.ingest_calls: list[dict] = []
        self.get_calls: list[list[dict]] = []
        self._responses: dict[str, deque] = {}

    def set_responses(self, attachment_id: str, *statuses):
        """`None` means "no document found" (empty documentDetails)."""
        self._responses[attachment_id] = deque(statuses)

    def get_knowledge_base_documents(self, *, knowledgeBaseId, dataSourceId, documentIdentifiers):
        self.get_calls.append(documentIdentifiers)
        attachment_id = documentIdentifiers[0]["custom"]["id"]
        queue = self._responses.get(attachment_id)
        if not queue:
            return {"documentDetails": []}
        status = queue[0]
        if len(queue) > 1:
            queue.popleft()
        if status is None:
            return {"documentDetails": []}
        return {"documentDetails": [{"status": status, "statusReason": None}]}

    def ingest_knowledge_base_documents(self, **kwargs):
        self.ingest_calls.append(kwargs)
        return {"documentDetails": []}


def s3_event_body(bucket=BUCKET, key=KEY, size=1024):
    return json.dumps({"Records": [{
        "eventName": "ObjectCreated:Put",
        "s3": {"bucket": {"name": bucket}, "object": {"key": key, "size": size}},
    }]})


def sqs_event(body, message_id="msg-1"):
    return {"Records": [{"messageId": message_id, "body": body}]}


@pytest.fixture(autouse=True)
def reset_clients(monkeypatch):
    handler._dynamodb = None
    handler._bedrock_agent = None
    handler._s3 = None
    # No real sleeping between polls in tests.
    monkeypatch.setattr(handler, "_sleep", lambda *_args: None)


@pytest.fixture
def table():
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="attachments", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"},
                       {"AttributeName": "attachment_id", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"},
                                  {"AttributeName": "attachment_id", "AttributeType": "S"}],
        )
        handler._dynamodb = ddb
        yield ddb.Table("attachments")


@pytest.fixture
def fake_bedrock():
    fake = FakeBedrockAgent()
    handler._bedrock_agent = fake
    return fake


def put_claim_row(table, *, attachment_id=ATTACHMENT_ID, user_id=USER_ID, session_id=SESSION_ID,
                  s3_key=KEY, content_type="application/pdf", status="pending_upload"):
    table.put_item(Item={
        "user_id": user_id, "attachment_id": attachment_id, "session_id": session_id,
        "filename": FILENAME, "content_type": content_type, "s3_key": s3_key,
        "status": status, "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00",
    })


def get_item(table, attachment_id=ATTACHMENT_ID, user_id=USER_ID):
    return table.get_item(Key={"user_id": user_id, "attachment_id": attachment_id}).get("Item")


class FakeContext:
    def __init__(self, remaining_ms=60_000):
        self._remaining_ms = remaining_ms

    def get_remaining_time_in_millis(self):
        return self._remaining_ms


def test_happy_path_ingests_with_correct_metadata_and_marks_ready(table, fake_bedrock):
    put_claim_row(table)
    fake_bedrock.set_responses(ATTACHMENT_ID, None, "INDEXED")

    result = handler.handler(sqs_event(s3_event_body()), FakeContext())

    assert result == {"batchItemFailures": []}
    assert len(fake_bedrock.ingest_calls) == 1
    document = fake_bedrock.ingest_calls[0]["documents"][0]
    assert document["content"]["dataSourceType"] == "CUSTOM"
    assert document["content"]["custom"]["customDocumentIdentifier"]["id"] == ATTACHMENT_ID
    assert document["content"]["custom"]["sourceType"] == "S3_LOCATION"
    assert document["content"]["custom"]["s3Location"]["uri"] == f"s3://{BUCKET}/{KEY}"
    attrs = {a["key"]: a["value"]["stringValue"] for a in document["metadata"]["inlineAttributes"]}
    assert attrs == {"user_id": USER_ID, "session_id": SESSION_ID, "file_id": ATTACHMENT_ID, "filename": FILENAME}
    assert get_item(table)["status"] == "ready"


def test_claim_mismatch_drops_without_ingest(table, fake_bedrock):
    put_claim_row(table, session_id=str(uuid.uuid4()))  # row exists for a different session

    result = handler.handler(sqs_event(s3_event_body()), FakeContext())

    assert result == {"batchItemFailures": []}
    assert fake_bedrock.ingest_calls == []
    assert fake_bedrock.get_calls == []
    assert get_item(table)["status"] == "pending_upload"


def test_invalid_key_shape_dropped(table, fake_bedrock):
    body = s3_event_body(key="uploads/not-enough-parts")

    result = handler.handler(sqs_event(body), FakeContext())

    assert result == {"batchItemFailures": []}
    assert fake_bedrock.ingest_calls == []


def test_s3_test_event_ignored(table, fake_bedrock):
    body = json.dumps({"Service": "Amazon S3", "Event": "s3:TestEvent", "Bucket": BUCKET})

    result = handler.handler(sqs_event(body), FakeContext())

    assert result == {"batchItemFailures": []}
    assert fake_bedrock.ingest_calls == []
    assert fake_bedrock.get_calls == []


def test_already_indexed_skips_reingest(table, fake_bedrock):
    put_claim_row(table)
    fake_bedrock.set_responses(ATTACHMENT_ID, "INDEXED")

    result = handler.handler(sqs_event(s3_event_body()), FakeContext())

    assert result == {"batchItemFailures": []}
    assert fake_bedrock.ingest_calls == []
    assert get_item(table)["status"] == "ready"


def test_pending_after_poll_reports_batch_item_failure(table, fake_bedrock):
    put_claim_row(table)
    fake_bedrock.set_responses(ATTACHMENT_ID, "IN_PROGRESS")

    result = handler.handler(sqs_event(s3_event_body(), message_id="msg-pending"), FakeContext(remaining_ms=20_000))

    assert result == {"batchItemFailures": [{"itemIdentifier": "msg-pending"}]}
    assert fake_bedrock.ingest_calls == []
    assert get_item(table)["status"] == "processing"


def test_failed_status_marks_failed_without_ingest(table, fake_bedrock):
    put_claim_row(table)
    fake_bedrock.set_responses(ATTACHMENT_ID, "FAILED")

    result = handler.handler(sqs_event(s3_event_body()), FakeContext())

    assert result == {"batchItemFailures": []}
    assert fake_bedrock.ingest_calls == []
    item = get_item(table)
    assert item["status"] == "failed"
    assert item["error_message"] == handler.ERR_INDEXING_FAILED


def test_unsupported_content_type_fails_without_ingest(table, fake_bedrock):
    put_claim_row(table, content_type="image/png")
    fake_bedrock.set_responses(ATTACHMENT_ID, None)

    result = handler.handler(sqs_event(s3_event_body()), FakeContext())

    assert result == {"batchItemFailures": []}
    assert fake_bedrock.ingest_calls == []
    item = get_item(table)
    assert item["status"] == "failed"
    assert item["error_message"] == handler.ERR_UNSUPPORTED_TYPE


def test_file_over_size_limit_fails_without_ingest(table, fake_bedrock):
    put_claim_row(table)
    fake_bedrock.set_responses(ATTACHMENT_ID, None)
    body = s3_event_body(size=handler.MAX_FILE_BYTES + 1)

    result = handler.handler(sqs_event(body), FakeContext())

    assert result == {"batchItemFailures": []}
    assert fake_bedrock.ingest_calls == []
    item = get_item(table)
    assert item["status"] == "failed"
    assert item["error_message"] == handler.ERR_FILE_TOO_LARGE
