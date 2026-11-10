"""Pre-signed URL generator for attachment uploads.

POST /attachments/presign
Request: { "filename": "paper.pdf", "content_type": "application/pdf", "session_id": "uuid" }
Response: { "upload_url": "https://...", "attachment_id": "uuid", "expires_in": 300 }

The caller uploads directly to S3 using the pre-signed URL. The attachment-processor
Lambda is triggered by S3 to extract text and index into the Knowledge Base.
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
MAX_SIZE_BYTES = 55 * 1024 * 1024  # 55 MB
PRESIGN_EXPIRY = 300  # 5 minutes

ALLOWED_TYPES = frozenset([
    "application/pdf",
    "text/plain",
    "text/markdown",
    "text/csv",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # .docx
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",  # .xlsx
])

_UUID_PATTERN = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

s3 = boto3.client("s3", config=Config(signature_version="s3v4"))
dynamodb = boto3.resource("dynamodb")


def _cors_headers() -> dict[str, str]:
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "Content-Type,Authorization",
        "Access-Control-Allow-Methods": "POST,OPTIONS",
    }


def _error(status: int, message: str) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {**_cors_headers(), "Content-Type": "application/json"},
        "body": json.dumps({"error": message}),
    }


def _success(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": 200,
        "headers": {**_cors_headers(), "Content-Type": "application/json"},
        "body": json.dumps(data),
    }


def _get_user_id(event: dict[str, Any]) -> str | None:
    claims = event.get("requestContext", {}).get("authorizer", {}).get("claims", {})
    return claims.get("sub")


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    if event.get("httpMethod") == "OPTIONS":
        return {"statusCode": 200, "headers": _cors_headers(), "body": ""}

    user_id = _get_user_id(event)
    if not user_id:
        return _error(401, "Authentication required")

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _error(400, "Invalid JSON body")

    filename = body.get("filename", "").strip()
    content_type = body.get("content_type", "").strip()
    session_id = body.get("session_id", "").strip()

    if not filename or len(filename) > 255:
        return _error(400, "filename is required (max 255 chars)")
    if not _UUID_PATTERN.match(session_id):
        return _error(400, "session_id must be a valid UUID")
    if content_type not in ALLOWED_TYPES:
        return _error(400, f"content_type must be one of: {', '.join(sorted(ALLOWED_TYPES))}")

    attachment_id = str(uuid.uuid4())
    s3_key = f"uploads/{user_id}/{session_id}/{attachment_id}/{filename}"

    # Generate pre-signed URL
    try:
        upload_url = s3.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": BUCKET,
                "Key": s3_key,
                "ContentType": content_type,
            },
            ExpiresIn=PRESIGN_EXPIRY,
        )
    except Exception as exc:
        logger.exception("Failed to generate pre-signed URL")
        return _error(500, "Failed to generate upload URL")

    # Record metadata
    now = datetime.now(UTC).isoformat()
    table = dynamodb.Table(TABLE)
    try:
        table.put_item(Item={
            "user_id": user_id,
            "attachment_id": attachment_id,
            "session_id": session_id,
            "filename": filename,
            "content_type": content_type,
            "s3_key": s3_key,
            "status": "pending_upload",
            "created_at": now,
            "updated_at": now,
        })
    except Exception as exc:
        logger.exception("Failed to record attachment metadata")
        return _error(500, "Failed to record attachment")

    logger.info({
        "event": "presign_generated",
        "user_id": user_id,
        "attachment_id": attachment_id,
        "filename": filename,
    })

    return _success({
        "upload_url": upload_url,
        "attachment_id": attachment_id,
        "s3_key": s3_key,
        "expires_in": PRESIGN_EXPIRY,
    })
