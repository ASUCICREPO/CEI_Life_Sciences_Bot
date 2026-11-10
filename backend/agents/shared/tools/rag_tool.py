"""RAG retrieval tool for chat attachments."""
from __future__ import annotations

import os
from typing import Any

import boto3
from langchain_core.tools import tool

BUCKET = os.environ.get("CEI_KB_BUCKET", "")
s3 = boto3.client("s3") if BUCKET else None


def _list_chunks(user_id: str, session_id: str | None = None) -> list[dict[str, Any]]:
    """List available chunk keys for a user/session."""
    if not s3:
        return []
    prefix = f"chunks/{user_id}/"
    if session_id:
        prefix = f"chunks/{user_id}/{session_id}/"
    
    chunks = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            chunks.append({"key": obj["Key"], "size": obj["Size"]})
    return chunks


def _read_chunk(key: str) -> str:
    """Read chunk content from S3."""
    if not s3:
        return ""
    resp = s3.get_object(Bucket=BUCKET, Key=key)
    return resp["Body"].read().decode("utf-8", errors="replace")


@tool
def retrieve_from_attachments(
    query: str,
    user_id: str,
    session_id: str | None = None,
    max_chunks: int = 10,
) -> str:
    """Search through user-uploaded attachments for relevant content.
    
    Args:
        query: Search query to find relevant content
        user_id: User ID to scope the search
        session_id: Optional session ID to limit search to specific chat
        max_chunks: Maximum number of chunks to return (default 10)
    
    Returns:
        Concatenated relevant text chunks from attachments
    """
    if not BUCKET or not s3:
        return "Attachment retrieval is not configured."
    
    chunks = _list_chunks(user_id, session_id)
    if not chunks:
        return "No attachments found for this user/session."
    
    # Simple approach: read chunks and do keyword matching
    # In production, use vector embeddings + similarity search
    query_lower = query.lower()
    query_words = set(query_lower.split())
    
    scored = []
    for chunk_info in chunks[:100]:  # Limit scanning
        try:
            content = _read_chunk(chunk_info["key"])
            content_lower = content.lower()
            # Simple relevance: count matching words
            score = sum(1 for word in query_words if word in content_lower)
            if score > 0:
                scored.append((score, content, chunk_info["key"]))
        except Exception:
            continue
    
    if not scored:
        return "No relevant content found in attachments."
    
    # Sort by score descending, take top chunks
    scored.sort(key=lambda x: -x[0])
    results = []
    for _, content, key in scored[:max_chunks]:
        # Extract filename from key
        parts = key.split("/")
        source = parts[-1] if parts else key
        results.append(f"[Source: {source}]\n{content}")
    
    return "\n\n---\n\n".join(results)
