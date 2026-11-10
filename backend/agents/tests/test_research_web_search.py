"""B44/F12: Research triage/retrieve/cite wiring for the Web Search connector.

Covers the triage prompt + allowed-source validation gate on `web_search_enabled()`, the
retrieve-node mapping of trimmed web results into `DataSourceResult`/`Citation`, failure
handling that must not crash the turn, and the citation rendering rule (url/title only, no
authors/year) in both `synthesize()` and `cite()`.
"""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from research.nodes import (
    _source_descriptions,
    _triage_system_prompt,
    cite,
    retrieve,
    synthesize,
    triage,
)
from research.state import TriagePlan
from shared.schemas.citation import Citation, SourceType
from shared.tools.base import DataSourceResult, GatewayUnavailableError


def _web_citation(source_id: str = "https://example.com/a", title: str = "Example Article") -> Citation:
    return Citation(
        source_id=source_id,
        source_type=SourceType.WEB,
        url=source_id,
        title=title,
        relevant_passage="The cited sentence.",
        retrieved_at=datetime.now(UTC),
        snippet="The cited sentence.",
    )


def _trimmed_payload() -> dict:
    citation = _web_citation()
    return {
        "source_type": "web",
        "results": [{"url": citation.url, "title": citation.title, "snippet": citation.snippet}],
        "citations": [citation.model_dump(mode="json")],
    }


# ── triage: prompt text gated on web_search_enabled() ───────────────────────


def test_source_descriptions_excludes_web_when_disabled(monkeypatch):
    monkeypatch.setattr("research.nodes.web_search_enabled", lambda: False)
    assert SourceType.WEB not in _source_descriptions()
    assert "web" not in _triage_system_prompt().split("Valid source keys: ")[1].split("\n")[0]


def test_source_descriptions_includes_web_when_enabled(monkeypatch):
    monkeypatch.setattr("research.nodes.web_search_enabled", lambda: True)
    assert SourceType.WEB in _source_descriptions()
    valid_keys_line = _triage_system_prompt().split("Valid source keys: ")[1].split("\n")[0]
    assert "web" in valid_keys_line


# ── triage: allowed-source validation strips a stray "web" pick when disabled ──


def _structured_model(plan: TriagePlan):
    raw = AIMessage(content="", usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})
    structured = Mock()
    structured.invoke.return_value = {"parsed": plan, "raw": raw}
    model = Mock()
    model.with_structured_output.return_value = structured
    return model


def test_triage_strips_web_selection_when_disabled(monkeypatch):
    monkeypatch.setattr("research.nodes.web_search_enabled", lambda: False)
    plan = TriagePlan(
        selected_sources=[SourceType.PUBMED, SourceType.WEB],
        source_queries={"pubmed": "aspirin", "web": "aspirin news"},
        reasoning="test",
    )
    monkeypatch.setattr("research.nodes.get_model", lambda **_: _structured_model(plan))

    result = triage({"messages": [HumanMessage(content="aspirin")]})

    assert result["triage_plan"].selected_sources == [SourceType.PUBMED]
    assert "web" not in result["triage_plan"].source_queries


def test_triage_keeps_web_selection_when_enabled(monkeypatch):
    monkeypatch.setattr("research.nodes.web_search_enabled", lambda: True)
    plan = TriagePlan(
        selected_sources=[SourceType.WEB],
        source_queries={"web": "latest news"},
        reasoning="test",
    )
    monkeypatch.setattr("research.nodes.get_model", lambda **_: _structured_model(plan))

    result = triage({"messages": [HumanMessage(content="latest news")]})

    assert result["triage_plan"].selected_sources == [SourceType.WEB]
    assert result["triage_plan"].source_queries == {"web": "latest news"}


# ── retrieve: web results map to DataSourceResult / Citation ───────────────


def test_retrieve_calls_web_search_and_maps_citations(monkeypatch):
    monkeypatch.setattr("research.nodes.web_search_enabled", lambda: True)
    captured = {}

    def fake_call(query: str, max_results: int = 5) -> dict:
        captured["query"] = query
        return _trimmed_payload()

    monkeypatch.setattr("research.nodes.call_web_search", fake_call)
    monkeypatch.setattr("research.nodes.ALL_DATA_SOURCE_TOOLS", [])

    plan = TriagePlan(selected_sources=[SourceType.WEB], source_queries={"web": "GLP-1 news"}, reasoning="test")
    response = retrieve({"query": "GLP-1 news", "triage_plan": plan})

    assert captured["query"] == "GLP-1 news"
    [result] = response["retrieved_results"]
    assert isinstance(result, DataSourceResult)
    assert result.source_type is SourceType.WEB
    assert result.results == [{"url": "https://example.com/a", "title": "Example Article", "snippet": "The cited sentence."}]
    [citation] = result.citations
    assert isinstance(citation, Citation)
    assert citation.source_type is SourceType.WEB
    assert citation.authors is None
    assert citation.year is None
    assert citation.snippet == "The cited sentence."
    assert "web" in response["trace"][0].detail


def test_retrieve_skips_web_when_disabled_and_does_not_crash(monkeypatch):
    monkeypatch.setattr("research.nodes.web_search_enabled", lambda: False)
    called = Mock()
    monkeypatch.setattr("research.nodes.call_web_search", called)
    monkeypatch.setattr("research.nodes.ALL_DATA_SOURCE_TOOLS", [])

    plan = TriagePlan(selected_sources=[SourceType.WEB], source_queries={"web": "q"}, reasoning="test")
    response = retrieve({"query": "q", "triage_plan": plan})

    called.assert_not_called()
    assert response["retrieved_results"] == []


def test_retrieve_web_failure_is_logged_and_does_not_crash_the_turn(monkeypatch):
    monkeypatch.setattr("research.nodes.web_search_enabled", lambda: True)

    def fake_call(query: str, max_results: int = 5):
        raise RuntimeError("upstream failure")

    monkeypatch.setattr("research.nodes.call_web_search", fake_call)
    monkeypatch.setattr("research.nodes.ALL_DATA_SOURCE_TOOLS", [])

    plan = TriagePlan(selected_sources=[SourceType.WEB], source_queries={"web": "q"}, reasoning="test")
    response = retrieve({"query": "q", "triage_plan": plan})

    assert response["retrieved_results"] == []
    assert "ERROR" in response["trace"][0].detail


def test_retrieve_web_gateway_unavailable_propagates(monkeypatch):
    monkeypatch.setattr("research.nodes.web_search_enabled", lambda: True)

    def fake_call(query: str, max_results: int = 5):
        raise GatewayUnavailableError("The data source gateway could not complete the request.")

    monkeypatch.setattr("research.nodes.call_web_search", fake_call)
    monkeypatch.setattr("research.nodes.ALL_DATA_SOURCE_TOOLS", [])

    plan = TriagePlan(selected_sources=[SourceType.WEB], source_queries={"web": "q"}, reasoning="test")
    with pytest.raises(GatewayUnavailableError):
        retrieve({"query": "q", "triage_plan": plan})


def test_retrieve_web_mixed_with_a_failing_data_source_keeps_both_results(monkeypatch):
    """A web failure must not stop sibling sources from retrieving in the same turn."""
    monkeypatch.setattr("research.nodes.web_search_enabled", lambda: True)
    monkeypatch.setattr("research.nodes.call_web_search", lambda query, max_results=5: _trimmed_payload())

    pubmed = Mock(source_type=SourceType.PUBMED)
    pubmed.invoke.return_value = DataSourceResult(source_type=SourceType.PUBMED, results=[], citations=[])
    monkeypatch.setattr("research.nodes.ALL_DATA_SOURCE_TOOLS", [pubmed])

    plan = TriagePlan(
        selected_sources=[SourceType.PUBMED, SourceType.WEB],
        source_queries={"pubmed": "aspirin", "web": "aspirin news"},
        reasoning="test",
    )
    response = retrieve({"query": "aspirin", "triage_plan": plan})

    source_types = {r.source_type for r in response["retrieved_results"]}
    assert source_types == {SourceType.PUBMED, SourceType.WEB}


# ── synthesize/cite: web citations render url/title only (no authors/year) ─


def test_synthesize_web_citation_omits_authors_and_year(monkeypatch):
    citation = _web_citation()
    retrieved = [DataSourceResult(source_type=SourceType.WEB, results=[], citations=[citation])]

    structured_model = Mock()
    structured_model.invoke.return_value = {"parsed": None, "raw": None, "parsing_error": "no tool call"}
    model = Mock()
    model.with_structured_output.return_value = structured_model
    model.invoke.return_value = AIMessage(content="answer")
    monkeypatch.setattr("research.nodes.get_model", lambda *a, **k: model)

    synthesize({"query": "q", "mode": "science", "retrieved_results": retrieved})

    user_prompt = model.invoke.call_args.args[0][1]["content"]
    assert f"[{citation.source_id}] {citation.title} — {citation.url}" in user_prompt
    assert "(None)" not in user_prompt


def test_cite_resolves_web_citation_without_authors_or_year():
    citation = _web_citation()
    result = cite({
        "synthesis": f"Relevant finding. [{citation.source_id}]",
        "retrieved_results": [DataSourceResult(source_type=SourceType.WEB, results=[], citations=[citation])],
    })

    text = result["messages"][-1].content
    assert f"[{citation.source_id}] {citation.title}. {citation.url}" in text
    assert "None" not in text
