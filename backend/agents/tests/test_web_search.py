"""B44: Web Search connector tool -- citation-trimming is the load-bearing rule under test.

Only `url`, `title`, and the cited snippet may ever leave `trim_web_search_results()` or survive
`redact_trace_event()`'s "sources" handling; the raw connector result list must never reach a
trace or a persisted citation.
"""
from __future__ import annotations

import pytest

from shared.schemas.citation import SourceType
from shared.streaming.trace_redaction import redact_trace_event
from shared.tools.gateway_mcp import GatewayCallError
from shared.tools.web_search import (
    WEB_SEARCH_CEDAR_ACTION,
    WEB_SEARCH_TARGET,
    WEB_SEARCH_TOOL_NAME,
    as_langchain_tool,
    trim_web_search_results,
    web_search_enabled,
)


RAW_PAYLOAD = {
    "results": [
        {
            "url": "https://example.com/a",
            "title": "Article A",
            "snippet": "The cited sentence.",
            # upstream fields that must never survive the trim (B44 -- no bulk storage)
            "full_text": "the entire scraped page" * 50,
            "rank": 1,
            "publisher_metadata": {"internal_id": "secret-123"},
        },
        {"url": "https://example.com/b", "title": "Article B", "text": "Alternate snippet key."},
        {"title": "Missing url, dropped"},
        {"url": "https://example.com/d"},
        "not-a-dict",
    ],
    "query_metadata": {"took_ms": 42},
}


def test_trim_keeps_only_url_title_snippet_per_hit():
    result = trim_web_search_results(RAW_PAYLOAD)
    assert [h.model_dump() for h in result.results] == [
        {"url": "https://example.com/a", "title": "Article A", "snippet": "The cited sentence."},
        {"url": "https://example.com/b", "title": "Article B", "snippet": "Alternate snippet key."},
    ]
    assert result.source_type is SourceType.WEB


def test_trim_builds_web_citations_with_snippet_only_metadata():
    result = trim_web_search_results(RAW_PAYLOAD)
    assert len(result.citations) == 2
    for citation in result.citations:
        assert citation.source_type is SourceType.WEB
        assert citation.authors is None
        assert citation.year is None
        assert citation.snippet


def test_trim_result_never_contains_the_raw_upstream_fields():
    result = trim_web_search_results(RAW_PAYLOAD)
    dumped = result.model_dump(mode="json")
    import json

    blob = json.dumps(dumped)
    assert "full_text" not in blob
    assert "secret-123" not in blob
    assert "rank" not in blob


def test_trim_handles_missing_or_malformed_results():
    assert trim_web_search_results({}).results == []
    assert trim_web_search_results({"results": "not-a-list"}).results == []


def test_run_fails_closed_without_a_token(monkeypatch):
    from shared.tools import web_search as module

    monkeypatch.setattr(module, "current_gateway_token", lambda: None)
    monkeypatch.setenv("CEI_TOOL_GATEWAY_URL", "https://example.invalid/mcp")
    with pytest.raises(GatewayCallError):
        module._run(query="GLP-1 agonists")


def test_run_calls_the_gateway_with_the_web_search_target_and_tool_name(monkeypatch):
    from shared.tools import web_search as module

    calls = []
    monkeypatch.setattr(module, "current_gateway_token", lambda: "token")
    monkeypatch.setenv("CEI_TOOL_GATEWAY_URL", "https://example.invalid/mcp")
    monkeypatch.setattr(
        module, "call_gateway_tool",
        lambda url, token, target, args, **kw: calls.append((target, kw["tool_name"], args)) or RAW_PAYLOAD,
    )
    output = module._run(query="GLP-1 agonists", max_results=3)
    assert calls == [(WEB_SEARCH_TARGET, WEB_SEARCH_TOOL_NAME, {"query": "GLP-1 agonists", "max_results": 3})]
    assert output["results"] == [
        {"url": "https://example.com/a", "title": "Article A", "snippet": "The cited sentence."},
        {"url": "https://example.com/b", "title": "Article B", "snippet": "Alternate snippet key."},
    ]


def test_web_search_enabled_reads_the_cdk_set_env_flag(monkeypatch):
    monkeypatch.delenv("CEI_WEB_SEARCH_ENABLED", raising=False)
    assert web_search_enabled() is False
    monkeypatch.setenv("CEI_WEB_SEARCH_ENABLED", "true")
    assert web_search_enabled() is True
    monkeypatch.setenv("CEI_WEB_SEARCH_ENABLED", "false")
    assert web_search_enabled() is False


def test_langchain_tool_has_a_stable_name_and_schema():
    tool = as_langchain_tool()
    assert tool.name == "web_search"
    assert set(tool.args_schema.model_fields) == {"query", "max_results"}


def test_cedar_action_name_matches_the_decision_log():
    # Illustrative action string recorded in .agents/decision-log.md / tool-gateway.ts comments --
    # the Cedar permit itself groups by WEB_SEARCH_TARGET only (see cedarPermit() in tool-gateway.ts).
    assert WEB_SEARCH_CEDAR_ACTION == "web-search___WebSearch"


# --- trace redaction: the trimming contract must hold at the central trace choke point too ---

FULL_UPSTREAM_SOURCE = {
    "source_id": "https://example.com/a",
    "source_type": "web",
    "url": "https://example.com/a",
    "title": "Article A",
    "snippet": "The cited sentence.",
    "relevant_passage": "The cited sentence.",
    "retrieved_at": "2026-11-10T00:00:00Z",
    "full_result_list": ["should", "never", "survive"],
}


def test_redact_trace_event_trims_a_web_source_to_url_title_snippet():
    event = {"sources": [FULL_UPSTREAM_SOURCE]}
    safe = redact_trace_event(event)
    assert safe == {"sources": [{
        "url": "https://example.com/a", "title": "Article A", "snippet": "The cited sentence.",
    }]}


def test_redact_trace_event_leaves_non_web_sources_untouched():
    event = {"sources": [{"source_type": "pubmed", "source_id": "PMID:1", "url": "x", "title": "y"}]}
    safe = redact_trace_event(event)
    assert safe["sources"][0]["source_id"] == "PMID:1"


def test_redact_trace_event_still_redacts_plain_string_sources():
    event = {"sources": ["arn:aws:secretsmanager:us-east-1:123456789012:secret:ncbi"]}
    safe = redact_trace_event(event)
    assert safe["sources"] == ["[redacted]"]
