"""RAG retrieval tool for user-uploaded attachments.

Queries the Bedrock Knowledge Base to find relevant content from the user's uploaded files.
Results are filtered by user_id to ensure data isolation.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import boto3
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

KB_ID = os.environ.get("CEI_KB_ID", "")

bedrock_agent_runtime = boto3.client("bedrock-agent-runtime")


@tool
def retrieve_from_attachments(query: str, user_id: str, session_id: str | None = None, max_results: int = 5) -> list[dict[str, Any]]:
    """Search through the user's uploaded documents for relevant information.

    Use this tool when the user asks about content from files they've uploaded,
    or when you need to reference their documents to answer a question.

    Args:
        query: The search query to find relevant content
        user_id: The user's ID (for data isolation)
        session_id: Optional session ID to scope to a specific chat's attachments
        max_results: Maximum number of results to return (default 5)

    Returns:
        List of relevant text chunks with source metadata
    """
    if not KB_ID:
        logger.warning("CEI_KB_ID not configured, RAG retrieval unavailable")
        return []

    # Build filter for user isolation
    filter_config: dict[str, Any] = {
        "equals": {"key": "user_id", "value": user_id}
    }
    if session_id:
        filter_config = {
            "andAll": [
                {"equals": {"key": "user_id", "value": user_id}},
                {"equals": {"key": "session_id", "value": session_id}},
            ]
        }

    try:
        response = bedrock_agent_runtime.retrieve(
            knowledgeBaseId=KB_ID,
            retrievalQuery={"text": query},
            retrievalConfiguration={
                "vectorSearchConfiguration": {
                    "numberOfResults": max_results,
                    "filter": filter_config,
                }
            },
        )

        results = []
        for item in response.get("retrievalResults", []):
            content = item.get("content", {}).get("text", "")
            metadata = item.get("metadata", {})
            score = item.get("score", 0.0)

            results.append({
                "content": content,
                "filename": metadata.get("filename", "unknown"),
                "attachment_id": metadata.get("attachment_id"),
                "score": score,
            })

        logger.info({
            "event": "rag_retrieval",
            "query_length": len(query),
            "results": len(results),
            "user_id": user_id[:8] + "..." if user_id else None,
        })

        return results

    except Exception as exc:
        logger.error(f"RAG retrieval failed: {exc}")
        return []


def get_rag_tools() -> list:
    """Return the list of RAG tools for agent registration."""
    return [retrieve_from_attachments]
