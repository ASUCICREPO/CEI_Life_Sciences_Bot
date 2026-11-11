"""Attachment processor — S3 trigger, extracts text, stores chunks for KB."""
from __future__ import annotations

import io
import logging
import os
import urllib.parse
from datetime import datetime, UTC
from typing import Any

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TABLE = os.environ.get("ATTACHMENTS_TABLE", "")
KB_BUCKET = os.environ.get("KB_BUCKET", "")

s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")


def _extract_pdf(content: bytes) -> str:
    try:
        import fitz
        doc = fitz.open(stream=content, filetype="pdf")
        return "\n\n".join(page.get_text() for page in doc)
    except Exception as e:
        logger.warning(f"PDF extraction failed: {e}")
        return ""


def _extract_docx(content: bytes) -> str:
    try:
        from docx import Document
        return "\n\n".join(p.text for p in Document(io.BytesIO(content)).paragraphs if p.text.strip())
    except Exception as e:
        logger.warning(f"DOCX extraction failed: {e}")
        return ""


def _extract(content: bytes, content_type: str, filename: str) -> str:
    if "pdf" in content_type.lower() or filename.lower().endswith(".pdf"):
        return _extract_pdf(content)
    if "wordprocessingml" in content_type or filename.lower().endswith(".docx"):
        return _extract_docx(content)
    return content.decode("utf-8", errors="replace")


def _chunk(text: str, size: int = 1000, overlap: int = 200) -> list[str]:
    chunks, start = [], 0
    while start < len(text):
        chunk = text[start:start + size]
        if chunk.strip():
            chunks.append(chunk)
        start += size - overlap
    return chunks


def _update_status(user_id: str, attachment_id: str, status: str, error: str | None = None) -> None:
    expr = "SET #s = :s, updated_at = :now"
    vals: dict[str, Any] = {":s": status, ":now": datetime.now(UTC).isoformat()}
    if error:
        expr += ", error_message = :e"
        vals[":e"] = error[:500]
    dynamodb.Table(TABLE).update_item(
        Key={"user_id": user_id, "attachment_id": attachment_id},
        UpdateExpression=expr, ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues=vals,
    )


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])
        parts = key.split("/")
        if len(parts) < 5 or parts[0] != "uploads":
            continue
        user_id, session_id, attachment_id, filename = parts[1], parts[2], parts[3], "/".join(parts[4:])
        logger.info({"event": "process", "attachment_id": attachment_id})

        try:
            _update_status(user_id, attachment_id, "processing")
            resp = s3.get_object(Bucket=bucket, Key=key)
            content = resp["Body"].read()
            text = _extract(content, resp.get("ContentType", ""), filename)
            if not text.strip():
                _update_status(user_id, attachment_id, "failed", "No text extracted")
                continue

            chunks = _chunk(text)
            for i, chunk in enumerate(chunks):
                chunk_key = f"chunks/{user_id}/{session_id}/{attachment_id}/chunk_{i:04d}.txt"
                s3.put_object(Bucket=KB_BUCKET, Key=chunk_key, Body=chunk.encode(), ContentType="text/plain",
                              Metadata={"user_id": user_id, "session_id": session_id, "attachment_id": attachment_id, "filename": filename})

            _update_status(user_id, attachment_id, "ready")
            logger.info({"event": "done", "attachment_id": attachment_id, "chunks": len(chunks)})
        except Exception as e:
            logger.exception(f"Failed: {key}")
            _update_status(user_id, attachment_id, "failed", str(e))

    return {"statusCode": 200}
