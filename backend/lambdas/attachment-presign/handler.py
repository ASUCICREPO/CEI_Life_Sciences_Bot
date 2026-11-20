"""Pre-signed POST generator for attachment uploads.

B32: S3 itself enforces the 55 MB cap. A presigned POST policy carries a `content-length-range`
condition, so S3 rejects a larger body before it stores the object. A presigned PUT cannot do this.
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, UTC
from typing import Any

import boto3
from botocore.config import Config

logger = logging.getLogger()
logger.setLevel(logging.INFO)

BUCKET = os.environ.get("ATTACHMENTS_BUCKET", "")
TABLE = os.environ.get("ATTACHMENTS_TABLE", "")
PRESIGN_EXPIRY = 300
# B32: 55 MB, in decimal bytes.
MAX_UPLOAD_BYTES = 55_000_000

ALLOWED_TYPES = frozenset([
    "application/pdf", "text/plain", "text/markdown", "text/csv",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
])

_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
# B112: the same run-start/write gate claim the REST Lambdas check via http_errors.require_age_verified;
# this Lambda carries no python-deps layer, so the check is inlined rather than imported.
AGE_VERIFIED_CLAIM = "cei_age_verified"

s3 = boto3.client("s3", config=Config(signature_version="s3v4"))
dynamodb = boto3.resource("dynamodb")


def _cors() -> dict[str, str]:
    return {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "Content-Type,Authorization", "Access-Control-Allow-Methods": "POST,OPTIONS"}


def _error(status: int, msg: str) -> dict[str, Any]:
    return {"statusCode": status, "headers": {**_cors(), "Content-Type": "application/json"}, "body": json.dumps({"error": msg})}


def _ok(data: dict[str, Any]) -> dict[str, Any]:
    return {"statusCode": 200, "headers": {**_cors(), "Content-Type": "application/json"}, "body": json.dumps(data)}


def _onboarding_required() -> dict[str, Any]:
    body = {"error": {"code": "onboarding_required", "message": "Complete your profile to continue."}}
    return {"statusCode": 403, "headers": {**_cors(), "Content-Type": "application/json"}, "body": json.dumps(body)}


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    if event.get("httpMethod") == "OPTIONS":
        return {"statusCode": 200, "headers": _cors(), "body": ""}

    claims = event.get("requestContext", {}).get("authorizer", {}).get("claims", {})
    user_id = claims.get("sub")
    if not user_id:
        return _error(401, "Authentication required")
    # B112: a presign is a run-adjacent write (it creates the pending attachment record).
    if claims.get(AGE_VERIFIED_CLAIM) != "true":
        return _onboarding_required()

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _error(400, "Invalid JSON")

    filename = body.get("filename", "").strip()[:255]
    content_type = body.get("content_type", "").strip()
    session_id = body.get("session_id", "").strip()

    if not filename:
        return _error(400, "filename required")
    if not _UUID.match(session_id):
        return _error(400, "session_id must be UUID")
    if content_type not in ALLOWED_TYPES:
        return _error(400, f"content_type must be one of: {', '.join(sorted(ALLOWED_TYPES))}")
    # Optional early check; S3 still enforces the cap through the POST policy below.
    size = body.get("size")
    if size is not None:
        if isinstance(size, bool) or not isinstance(size, int) or size < 1:
            return _error(400, "size must be a positive integer")
        if size > MAX_UPLOAD_BYTES:
            return _error(400, f"File too large (max {MAX_UPLOAD_BYTES} bytes)")

    attachment_id = str(uuid.uuid4())
    s3_key = f"uploads/{user_id}/{session_id}/{attachment_id}/{filename}"

    try:
        post = s3.generate_presigned_post(
            Bucket=BUCKET,
            Key=s3_key,
            Fields={"Content-Type": content_type},
            Conditions=[
                {"Content-Type": content_type},
                ["content-length-range", 1, MAX_UPLOAD_BYTES],
            ],
            ExpiresIn=PRESIGN_EXPIRY,
        )
    except Exception:
        logger.exception("presign failed")
        return _error(500, "Failed to generate upload URL")

    now = datetime.now(UTC).isoformat()
    try:
        dynamodb.Table(TABLE).put_item(Item={
            "user_id": user_id, "attachment_id": attachment_id, "session_id": session_id,
            "filename": filename, "content_type": content_type, "s3_key": s3_key,
            "status": "pending_upload", "created_at": now, "updated_at": now,
        })
    except Exception:
        logger.exception("metadata write failed")
        return _error(500, "Failed to record attachment")

    logger.info({"event": "presign", "user_id": user_id[:8], "attachment_id": attachment_id})
    return _ok({
        "upload_url": post["url"], "upload_fields": post["fields"], "max_bytes": MAX_UPLOAD_BYTES,
        "attachment_id": attachment_id, "s3_key": s3_key, "expires_in": PRESIGN_EXPIRY,
    })
