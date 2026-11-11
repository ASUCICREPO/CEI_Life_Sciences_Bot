"""Managed Knowledge Base retrieval for per-chat attachments (B110, supersedes B33/B77).

A deterministic graph step, never a model-exposed tool: callers (research/drafting/analysis
nodes) pass `user_id`/`session_id` straight from `BaseAgentState`, which is populated only from
the verified `AuthorizedSession` at the entry point -- the model supplies only the query text and
can never see or change the filter. Mirrors the retrieval shape of `shared/tools/web_search.py`
(no DataSourceTool/Lambda, results mapped to the same `DataSourceResult`/`Citation` contract).
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

from shared.persistence.ownership import is_session_uuid
from shared.schemas.citation import Citation, SourceType
from shared.tools.base import DataSourceResult

logger = logging.getLogger(__name__)

# Cognito `sub` shape; deliberately looser than the session UUID check below.
_USER_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,128}$")

_default_client: Any = None


def _get_default_client() -> Any:
    # ADR: lazy module-level client | local/test runs never construct boto3 just by importing this
    # module; only a real, feature-enabled call pays for the client (same pattern as base.py).
    global _default_client
    if _default_client is None:
        import boto3

        _default_client = boto3.client("bedrock-agent-runtime")
    return _default_client


def retrieve_attachments(
    query: str,
    *,
    user_id: str,
    session_id: str,
    max_results: int = 8,
    client: Any = None,
) -> DataSourceResult | None:
    """Retrieve this chat's attachment chunks for `query`, scoped to `user_id` + `session_id`.

    Returns None when the Knowledge Base is not configured (`CEI_KNOWLEDGE_BASE_ID` unset, e.g.
    local runs), either id fails format validation, the retrieve call errors, or nothing matched.
    Callers append the result to `retrieved_results` only when it is not None, same as web search.
    """
    kb_id = os.environ.get("CEI_KNOWLEDGE_BASE_ID", "").strip()
    if not kb_id:
        return None
    if not isinstance(user_id, str) or not _USER_ID_RE.fullmatch(user_id):
        logger.warning({"event": "attachments_retrieve_invalid_id", "field": "user_id"})
        return None
    if not is_session_uuid(session_id):
        logger.warning({"event": "attachments_retrieve_invalid_id", "field": "session_id"})
        return None

    bedrock_client = client if client is not None else _get_default_client()
    try:
        response = bedrock_client.retrieve(
            knowledgeBaseId=kb_id,
            retrievalQuery={"text": query},
            retrievalConfiguration={
                "managedSearchConfiguration": {
                    "numberOfResults": max_results,
                    # ADR: filter built only from the arguments above | the caller (model) never
                    # supplies or widens this filter -- B110's load-bearing guarantee.
                    "filter": {
                        "andAll": [
                            {"equals": {"key": "user_id", "value": user_id}},
                            {"equals": {"key": "session_id", "value": session_id}},
                        ]
                    },
                }
            },
        )
    except Exception as exc:  # noqa: BLE001 -- never raise into the graph; treat as "no attachments"
        logger.error({"event": "attachments_retrieve_error", "error_type": type(exc).__name__})
        return None

    results: list[dict[str, Any]] = []
    citations: list[Citation] = []
    retrieved_at = datetime.now(timezone.utc)
    for hit in response.get("retrievalResults", []) or []:
        metadata = hit.get("metadata") or {}
        # ADR: defense in depth | the server-side filter above already scopes the call; drop any
        # chunk whose own metadata disagrees rather than trust the filter alone (B110).
        if str(metadata.get("user_id", "")) != user_id or str(metadata.get("session_id", "")) != session_id:
            continue
        text = (hit.get("content") or {}).get("text", "")
        if not text:
            continue
        file_id = str(metadata.get("file_id", ""))
        filename = str(metadata.get("filename") or file_id or "attachment")
        results.append({"file_id": file_id, "filename": filename, "text": text, "score": hit.get("score")})
        citations.append(
            Citation(
                source_id=f"attachment:{file_id}:{len(citations)}",
                source_type=SourceType.ATTACHMENTS,
                url="",  # B110: attachments have no external URL; the filename is the citation title.
                title=filename,
                relevant_passage=text,
                retrieved_at=retrieved_at,
            )
        )

    if not results:
        return None

    logger.info({"event": "attachments_retrieve_success", "result_count": len(results)})
    return DataSourceResult(source_type=SourceType.ATTACHMENTS, results=results, citations=citations)
