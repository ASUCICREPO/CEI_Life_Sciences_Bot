"""Attachment processor -- SQS-triggered Managed Knowledge Base ingestion (B110).

Upload flow: S3 -> SQS (+DLQ) -> this Lambda. Each SQS record body is an S3 event
notification (may be an `s3:TestEvent`, which carries no object and is ignored).
The processor re-validates the upload key shape and the attachments-table claim
row (B31) before calling `IngestKnowledgeBaseDocuments` on the knowledge base's
CUSTOM data source, then polls `GetKnowledgeBaseDocuments` for a bounded time.
Retrieval-time filtering by user_id/session_id is runtime code (B110), not here.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.parse
from datetime import UTC, datetime
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TABLE = os.environ.get("ATTACHMENTS_TABLE", "")
BUCKET = os.environ.get("ATTACHMENTS_BUCKET", "")
KNOWLEDGE_BASE_ID = os.environ.get("KNOWLEDGE_BASE_ID", "")
DATA_SOURCE_ID = os.environ.get("KNOWLEDGE_BASE_DATA_SOURCE_ID", "")

_USER_ID = re.compile(r"^[A-Za-z0-9-]{1,128}$")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

# ADR: mirror attachment-presign's ALLOWED_TYPES | Rationale: these are also the formats the Managed KB
# direct-ingestion API accepts (docs.aws.amazon.com/bedrock/.../knowledge-base-ds.html, read 2026-11-11)
_SUPPORTED_CONTENT_TYPES = frozenset([
    "application/pdf", "text/plain", "text/markdown", "text/csv", "text/html",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
])
# Managed KB per-file quota (same doc page); image files (.jpeg/.png) have a separate, smaller 3.75 MB
# quota, but B32's image types are not in _SUPPORTED_CONTENT_TYPES above and never reach this Lambda.
MAX_FILE_BYTES = 50 * 1024 * 1024

_READY_STATUSES = frozenset({"INDEXED", "PARTIALLY_INDEXED", "METADATA_PARTIALLY_INDEXED"})
_FAILED_STATUSES = frozenset({"FAILED", "METADATA_UPDATE_FAILED", "IGNORED", "DELETING", "DELETE_IN_PROGRESS"})
# PENDING / STARTING / IN_PROGRESS / NOT_FOUND (just after a fresh ingest call) are all non-terminal.

POLL_INTERVAL_SECONDS = 3
MAX_POLL_MS = 90_000
POLL_SAFETY_MARGIN_MS = 10_000

ERR_UNSUPPORTED_TYPE = "unsupported_file_type"
ERR_FILE_TOO_LARGE = "file_too_large"
ERR_INDEXING_FAILED = "indexing_failed"

# ADR: lazy clients | Rationale: tests inject stubs (botocore.stub.Stubber / fakes) by assigning these
# module globals before calling handler(), without needing real AWS credentials at import time.
_s3 = None
_dynamodb = None
_bedrock_agent = None
_sleep = time.sleep


def _s3_client():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3")
    return _s3


def _attachments_table():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb")
    return _dynamodb.Table(TABLE)


def _bedrock_agent_client():
    global _bedrock_agent
    if _bedrock_agent is None:
        _bedrock_agent = boto3.client("bedrock-agent")
    return _bedrock_agent


def _update_status(user_id: str, attachment_id: str, status: str, error_code: str | None = None) -> None:
    expr = "SET #s = :s, updated_at = :now"
    names = {"#s": "status"}
    vals: dict[str, Any] = {":s": status, ":now": datetime.now(UTC).isoformat()}
    if error_code:
        expr += ", error_message = :e"
        vals[":e"] = error_code
    _attachments_table().update_item(
        Key={"user_id": user_id, "attachment_id": attachment_id},
        UpdateExpression=expr, ExpressionAttributeNames=names, ExpressionAttributeValues=vals,
    )


def _parse_key(key: str) -> tuple[str, str, str, str] | None:
    """Return (user_id, session_id, attachment_id, filename), or None if the shape is invalid."""
    parts = key.split("/")
    if len(parts) < 5 or parts[0] != "uploads":
        return None
    user_id, session_id, attachment_id = parts[1], parts[2], parts[3]
    filename = "/".join(parts[4:])
    if not filename or not _USER_ID.match(user_id) or not _UUID.match(session_id) or not _UUID.match(attachment_id):
        return None
    return user_id, session_id, attachment_id, filename


def _claim_matches(item: dict[str, Any] | None, session_id: str, key: str) -> bool:
    return bool(item) and item.get("session_id") == session_id and item.get("s3_key") == key


def _document_identifier(attachment_id: str) -> dict[str, Any]:
    return {"dataSourceType": "CUSTOM", "custom": {"id": attachment_id}}


def _lookup_status(attachment_id: str) -> tuple[str, str | None]:
    try:
        response = _bedrock_agent_client().get_knowledge_base_documents(
            knowledgeBaseId=KNOWLEDGE_BASE_ID, dataSourceId=DATA_SOURCE_ID,
            documentIdentifiers=[_document_identifier(attachment_id)],
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            return "NOT_FOUND", None
        raise
    details = response.get("documentDetails") or []
    if not details:
        return "NOT_FOUND", None
    detail = details[0]
    return detail.get("status", "NOT_FOUND"), detail.get("statusReason")


def _ingest(attachment_id: str, user_id: str, session_id: str, filename: str, bucket: str, key: str) -> None:
    _bedrock_agent_client().ingest_knowledge_base_documents(
        knowledgeBaseId=KNOWLEDGE_BASE_ID, dataSourceId=DATA_SOURCE_ID,
        documents=[{
            "content": {
                "dataSourceType": "CUSTOM",
                "custom": {
                    "customDocumentIdentifier": {"id": attachment_id},
                    "sourceType": "S3_LOCATION",
                    "s3Location": {"uri": f"s3://{bucket}/{key}"},
                },
            },
            "metadata": {
                "type": "IN_LINE_ATTRIBUTE",
                "inlineAttributes": [
                    {"key": "user_id", "value": {"type": "STRING", "stringValue": user_id}},
                    {"key": "session_id", "value": {"type": "STRING", "stringValue": session_id}},
                    {"key": "file_id", "value": {"type": "STRING", "stringValue": attachment_id}},
                    {"key": "filename", "value": {"type": "STRING", "stringValue": filename}},
                ],
            },
        }],
    )


def _poll_budget_ms(context: Any) -> int:
    if context is not None:
        try:
            remaining = context.get_remaining_time_in_millis() - POLL_SAFETY_MARGIN_MS
            return max(0, min(remaining, MAX_POLL_MS))
        except AttributeError:
            pass
    return MAX_POLL_MS


def _poll_until_resolved(context: Any, attachment_id: str) -> tuple[str, str | None]:
    budget_ms = _poll_budget_ms(context)
    elapsed_ms = 0
    while True:
        status, reason = _lookup_status(attachment_id)
        if status in _READY_STATUSES or status in _FAILED_STATUSES:
            return status, reason
        if elapsed_ms >= budget_ms:
            return status, reason
        _sleep(POLL_INTERVAL_SECONDS)
        elapsed_ms += POLL_INTERVAL_SECONDS * 1000


def _finalize(user_id: str, attachment_id: str, status: str) -> bool:
    """Returns True if the SQS message should be retried."""
    if status in _READY_STATUSES:
        _update_status(user_id, attachment_id, "ready")
        return False
    if status in _FAILED_STATUSES:
        _update_status(user_id, attachment_id, "failed", ERR_INDEXING_FAILED)
        return False
    logger.info(json.dumps({"event": "ingest_pending_retry", "attachment_id": attachment_id, "status": status}))
    return True


def _object_size(record: dict[str, Any], bucket: str, key: str) -> int | None:
    size = record.get("s3", {}).get("object", {}).get("size")
    if isinstance(size, int):
        return size
    try:
        return _s3_client().head_object(Bucket=bucket, Key=key).get("ContentLength")
    except ClientError:
        return None


def _process_s3_record(record: dict[str, Any], context: Any) -> bool:
    """Returns True if the SQS message carrying this S3 record should be retried."""
    bucket = record.get("s3", {}).get("bucket", {}).get("name")
    raw_key = record.get("s3", {}).get("object", {}).get("key")
    if not bucket or not raw_key:
        logger.warning(json.dumps({"event": "invalid_s3_record"}))
        return False
    key = urllib.parse.unquote_plus(raw_key)

    if bucket != BUCKET:
        logger.warning(json.dumps({"event": "unexpected_bucket"}))
        return False

    parsed = _parse_key(key)
    if parsed is None:
        logger.warning(json.dumps({"event": "invalid_upload_key_shape"}))
        return False
    user_id, session_id, attachment_id, filename = parsed

    item = _attachments_table().get_item(Key={"user_id": user_id, "attachment_id": attachment_id}).get("Item")
    if not _claim_matches(item, session_id, key):
        logger.warning(json.dumps({"event": "attachment_claim_mismatch", "attachment_id": attachment_id}))
        return False

    _update_status(user_id, attachment_id, "processing")
    logger.info(json.dumps({"event": "process_attachment", "attachment_id": attachment_id}))

    status, _reason = _lookup_status(attachment_id)

    if status in _READY_STATUSES or status in _FAILED_STATUSES:
        return _finalize(user_id, attachment_id, status)

    needs_ingest = status == "NOT_FOUND"
    if needs_ingest:
        content_type = item.get("content_type") if item else None
        if content_type not in _SUPPORTED_CONTENT_TYPES:
            logger.warning(json.dumps({"event": "unsupported_content_type", "attachment_id": attachment_id}))
            _update_status(user_id, attachment_id, "failed", ERR_UNSUPPORTED_TYPE)
            return False

        size = _object_size(record, bucket, key)
        if isinstance(size, int) and size > MAX_FILE_BYTES:
            logger.warning(json.dumps({"event": "file_too_large", "attachment_id": attachment_id}))
            _update_status(user_id, attachment_id, "failed", ERR_FILE_TOO_LARGE)
            return False

        _ingest(attachment_id, user_id, session_id, filename, bucket, key)
        logger.info(json.dumps({"event": "ingest_submitted", "attachment_id": attachment_id}))

    final_status, _reason = _poll_until_resolved(context, attachment_id)
    return _finalize(user_id, attachment_id, final_status)


def _process_message(record: dict[str, Any], context: Any) -> bool:
    """Returns True if this SQS message should be retried (reported as a batch item failure)."""
    try:
        body = json.loads(record["body"])
    except (KeyError, json.JSONDecodeError):
        logger.warning(json.dumps({"event": "invalid_message_body"}))
        return False

    if not isinstance(body, dict):
        logger.warning(json.dumps({"event": "invalid_message_body"}))
        return False

    if body.get("Event") == "s3:TestEvent":
        logger.info(json.dumps({"event": "s3_test_event_ignored"}))
        return False

    s3_records = body.get("Records")
    if not isinstance(s3_records, list) or not s3_records:
        logger.warning(json.dumps({"event": "invalid_s3_notification"}))
        return False

    retry = False
    for s3_record in s3_records:
        if _process_s3_record(s3_record, context):
            retry = True
    return retry


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    failures: list[dict[str, str]] = []
    for record in event.get("Records", []):
        message_id = record.get("messageId")
        try:
            retry = _process_message(record, context)
        except Exception:
            logger.exception("attachment_processor_unhandled_error")
            retry = True
        if retry and message_id:
            failures.append({"itemIdentifier": message_id})
    return {"batchItemFailures": failures}
