"""Attachment processor — triggered by S3 uploads.

Extracts text from uploaded files, chunks it, and stores in S3 for Bedrock KB ingestion.
Updates the attachment metadata table with processing status.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import urllib.parse
from datetime import datetime, UTC
from typing import Any

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ATTACHMENTS_TABLE = os.environ.get("ATTACHMENTS_TABLE", "")
KB_BUCKET = os.environ.get("KB_BUCKET", "")  # Where extracted text goes for KB ingestion
KB_ID = os.environ.get("KB_ID", "")
KB_DATA_SOURCE_ID = os.environ.get("KB_DATA_SOURCE_ID", "")

s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
bedrock_agent = boto3.client("bedrock-agent")

_UUID_PATTERN = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def _extract_text_pdf(content: bytes) -> str:
    """Extract text from PDF using PyMuPDF if available, else skip."""
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(stream=content, filetype="pdf")
        text_parts = []
        for page in doc:
            text_parts.append(page.get_text())
        return "\n\n".join(text_parts)
    except ImportError:
        logger.warning("PyMuPDF not available, returning empty text for PDF")
        return ""
    except Exception as exc:
        logger.error(f"PDF extraction failed: {exc}")
        return ""


def _extract_text_docx(content: bytes) -> str:
    """Extract text from DOCX."""
    try:
        from docx import Document
        doc = Document(io.BytesIO(content))
        return "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())
    except ImportError:
        logger.warning("python-docx not available")
        return ""
    except Exception as exc:
        logger.error(f"DOCX extraction failed: {exc}")
        return ""


def _extract_text(content: bytes, content_type: str, filename: str) -> str:
    """Extract text based on content type."""
    if content_type == "application/pdf" or filename.lower().endswith(".pdf"):
        return _extract_text_pdf(content)
    elif content_type in ("text/plain", "text/markdown", "text/csv"):
        return content.decode("utf-8", errors="replace")
    elif "wordprocessingml" in content_type or filename.lower().endswith(".docx"):
        return _extract_text_docx(content)
    else:
        # Try as plain text
        return content.decode("utf-8", errors="replace")


def _chunk_text(text: str, chunk_size: int = 1000, overlap: int = 200) -> list[str]:
    """Split text into overlapping chunks."""
    if not text:
        return []
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end]
        if chunk.strip():
            chunks.append(chunk)
        start = end - overlap
    return chunks


def _update_status(user_id: str, attachment_id: str, status: str, error: str | None = None) -> None:
    """Update attachment processing status."""
    table = dynamodb.Table(ATTACHMENTS_TABLE)
    update_expr = "SET #status = :status, updated_at = :now"
    expr_values: dict[str, Any] = {
        ":status": status,
        ":now": datetime.now(UTC).isoformat(),
    }
    if error:
        update_expr += ", error_message = :error"
        expr_values[":error"] = error[:500]
    
    table.update_item(
        Key={"user_id": user_id, "attachment_id": attachment_id},
        UpdateExpression=update_expr,
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues=expr_values,
    )


def _parse_s3_key(key: str) -> tuple[str, str, str, str] | None:
    """Parse uploads/{user_id}/{session_id}/{attachment_id}/{filename}."""
    parts = key.split("/")
    if len(parts) >= 5 and parts[0] == "uploads":
        return parts[1], parts[2], parts[3], "/".join(parts[4:])
    return None


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Process S3 event notifications."""
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])
        
        parsed = _parse_s3_key(key)
        if not parsed:
            logger.warning(f"Skipping non-upload key: {key}")
            continue
        
        user_id, session_id, attachment_id, filename = parsed
        logger.info({
            "event": "processing_attachment",
            "user_id": user_id,
            "attachment_id": attachment_id,
            "filename": filename,
        })

        try:
            # Update status to processing
            _update_status(user_id, attachment_id, "processing")

            # Download file
            response = s3.get_object(Bucket=bucket, Key=key)
            content = response["Body"].read()
            content_type = response.get("ContentType", "application/octet-stream")

            # Extract text
            text = _extract_text(content, content_type, filename)
            if not text.strip():
                _update_status(user_id, attachment_id, "failed", "No text could be extracted")
                continue

            # Chunk text
            chunks = _chunk_text(text)
            logger.info(f"Extracted {len(text)} chars, {len(chunks)} chunks from {filename}")

            # Store chunks in KB bucket with metadata for filtering
            for i, chunk in enumerate(chunks):
                chunk_key = f"chunks/{user_id}/{session_id}/{attachment_id}/chunk_{i:04d}.txt"
                metadata = {
                    "user_id": user_id,
                    "session_id": session_id,
                    "attachment_id": attachment_id,
                    "filename": filename,
                    "chunk_index": str(i),
                }
                s3.put_object(
                    Bucket=KB_BUCKET,
                    Key=chunk_key,
                    Body=chunk.encode("utf-8"),
                    ContentType="text/plain",
                    Metadata=metadata,
                )

            # Trigger KB sync if configured
            if KB_ID and KB_DATA_SOURCE_ID:
                try:
                    bedrock_agent.start_ingestion_job(
                        knowledgeBaseId=KB_ID,
                        dataSourceId=KB_DATA_SOURCE_ID,
                    )
                    logger.info(f"Started KB ingestion job for {attachment_id}")
                except Exception as exc:
                    logger.warning(f"KB ingestion trigger failed (non-fatal): {exc}")

            # Update status to ready
            _update_status(user_id, attachment_id, "ready")
            logger.info({
                "event": "attachment_processed",
                "attachment_id": attachment_id,
                "chunks": len(chunks),
            })

        except Exception as exc:
            logger.exception(f"Failed to process {key}")
            _update_status(user_id, attachment_id, "failed", str(exc))

    return {"statusCode": 200}
