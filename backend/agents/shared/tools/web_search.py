"""AgentCore Web Search connector tool (B44).

A built-in Gateway connector, not a custom Lambda target (B86) -- but it is still a Gateway
target, so it is reached through the same custom-JWT `call_gateway_tool` MCP path every
specialist agent already uses for the ten data-source targets (`shared/tools/base.py`), not the
SigV4 `bedrock_agentcore` SDK client (that client only fits IAM-direct connector use, not this
Gateway's Cognito-JWT inbound auth).

Citation storage rule (B44): only `url`, `title`, and the cited snippet are ever carried past this
module -- never the raw result list. `trim_web_search_results()` is the one function allowed to
turn a raw connector payload into stored/returned data; `shared/streaming/trace_redaction.py`
re-applies the same trim to every `sources` trace field as a second line of defense.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from shared.schemas.citation import Citation, SourceType
from shared.tools.gateway_context import current_gateway_token
from shared.tools.gateway_mcp import GatewayCallError, call_gateway_tool

logger = logging.getLogger(__name__)

# B44/B86: Gateway target id for the built-in connector; Cedar groups the permit by this name
# (see `cedarPermit()` in backend/lib/constructs/cedar-policy.ts), not by the specific tool name.
WEB_SEARCH_TARGET = "web-search"
# Installed-SDK fact (bedrock_agentcore.tools.web_search_client): the connector exposes a tool
# named "WebSearch". TBD per tools.md section 4 / section 9: S2 must confirm this is also the
# exact name the built-in connector registers on *this* custom-JWT Gateway's tools/list.
WEB_SEARCH_TOOL_NAME = "WebSearch"
# Matches the Cedar permit action used in backend/lib/constructs/tool-gateway.ts and recorded in
# .agents/decision-log.md; not sent on the wire (the MCP call uses WEB_SEARCH_TARGET/WEB_SEARCH_TOOL_NAME).
WEB_SEARCH_CEDAR_ACTION = f"{WEB_SEARCH_TARGET}___{WEB_SEARCH_TOOL_NAME}"


class WebSearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=400)
    max_results: int = Field(default=5, ge=1, le=20)


class WebSearchHit(BaseModel):
    """One trimmed web search result: url/title/snippet only (B44 -- never the raw upstream fields)."""

    url: str
    title: str
    snippet: str


class WebSearchResult(BaseModel):
    source_type: SourceType = SourceType.WEB
    results: list[WebSearchHit]
    citations: list[Citation]


def _coerce_hit(raw: Any) -> WebSearchHit | None:
    if not isinstance(raw, dict):
        return None
    url = raw.get("url")
    title = raw.get("title")
    snippet = raw.get("snippet") or raw.get("text") or ""
    if not isinstance(url, str) or not url or not isinstance(title, str) or not title:
        return None
    return WebSearchHit(url=url, title=title, snippet=str(snippet))


def trim_web_search_results(payload: dict[str, Any]) -> WebSearchResult:
    """B44: the only function allowed to turn a raw connector payload into stored/returned data.

    Drops every upstream field except `url`, `title`, and the snippet -- the raw `results` list
    from `payload` never reaches a caller past this boundary.
    """
    raw_results = payload.get("results", []) if isinstance(payload, dict) else []
    hits = [hit for hit in (_coerce_hit(item) for item in raw_results) if hit is not None]
    now = datetime.now(timezone.utc)
    citations = [
        Citation(
            source_id=hit.url,
            source_type=SourceType.WEB,
            url=hit.url,
            title=hit.title,
            relevant_passage=hit.snippet,
            snippet=hit.snippet,
            retrieved_at=now,
        )
        for hit in hits
    ]
    return WebSearchResult(results=hits, citations=citations)


def _run(query: str, max_results: int = 5) -> dict[str, Any]:
    token = current_gateway_token()
    endpoint = os.environ.get("CEI_TOOL_GATEWAY_URL", "")
    if not token or not endpoint:
        raise GatewayCallError("The web search gateway is unavailable.")
    payload = call_gateway_tool(
        endpoint, token, WEB_SEARCH_TARGET, {"query": query, "max_results": max_results},
        tool_name=WEB_SEARCH_TOOL_NAME,
    )
    result = trim_web_search_results(payload)
    # Structured JSON log, never the raw result list (B44) -- only a count.
    logger.info({"event": "web_search_call", "result_count": len(result.results)})
    return result.model_dump(mode="json")


def web_search_enabled() -> bool:
    """CDK sets this only when the region gate (B44/B45) allowed the Gateway target to be built."""
    return os.environ.get("CEI_WEB_SEARCH_ENABLED", "").strip().lower() == "true"


def as_langchain_tool() -> StructuredTool:
    return StructuredTool.from_function(
        func=_run,
        name="web_search",
        description=(
            "Search the public web for current information outside the life-sciences data "
            "sources. Returns only the url, title, and snippet of each result (no bulk result "
            "storage, B44)."
        ),
        args_schema=WebSearchInput,
    )
