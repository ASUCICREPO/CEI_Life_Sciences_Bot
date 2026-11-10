from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import Mock

from langchain_core.messages import AIMessage, HumanMessage

from research.nodes import _TRIAGE_SYSTEM, cite, retrieve, synthesize, triage
from research.state import TriagePlan
from shared.schemas.citation import Citation, SourceType
from shared.schemas.response_blocks import ProseBlock, ResponseDocument, ResponseSection
from shared.tools.base import DataSourceResult


def _pubmed_result(*, citations: list[Citation] | None = None) -> DataSourceResult:
    return DataSourceResult(
        source_type=SourceType.PUBMED,
        results=[{"title": "Aspirin evidence"}],
        citations=citations or [],
    )


def _citation() -> Citation:
    return Citation(
        source_id="PMID:1",
        source_type=SourceType.PUBMED,
        url="https://pubmed.ncbi.nlm.nih.gov/1/",
        title="Aspirin evidence",
        authors=["Researcher"],
        year=2026,
        relevant_passage="Evidence passage.",
        retrieved_at=datetime.now(UTC),
    )


def test_triage_prompt_ignores_presentation_phrasing(monkeypatch) -> None:
    raw_response = AIMessage(content="", usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})
    structured_model = Mock()
    structured_model.invoke.return_value = {
        "parsed": TriagePlan(selected_sources=[], reasoning="presentation request"),
        "raw": raw_response,
    }
    model = Mock()
    model.with_structured_output.return_value = structured_model
    factory = Mock(return_value=model)
    monkeypatch.setattr("research.nodes.get_model", factory)

    triage({"messages": [HumanMessage(content="Use UI protocol for aspirin research")], "model_id": "turn-model"})
    factory.assert_called_once_with(model_id="turn-model")

    system_prompt = structured_model.invoke.call_args.args[0][0]["content"].lower()
    for phrase in ("ui protocol", "show ui elements", "interactive graphics", "charts", "output formatting"):
        assert phrase in system_prompt
    assert "substantive life-sciences research question" in system_prompt


def test_retrieve_falls_back_to_pubmed_and_preserves_citations(monkeypatch) -> None:
    pubmed = Mock(source_type=SourceType.PUBMED)
    result = _pubmed_result(citations=[_citation()])
    pubmed.invoke.return_value = result
    monkeypatch.setattr("research.nodes.ALL_DATA_SOURCE_TOOLS", [pubmed])

    response = retrieve({
        "query": "What evidence supports aspirin use? Show UI elements and charts.",
        "triage_plan": TriagePlan(selected_sources=[], reasoning="no source"),
    })

    pubmed.invoke.assert_called_once_with(query="What evidence supports aspirin use?")
    assert response["retrieved_results"] == [result]
    assert response["retrieved_results"][0].citations[0].source_id == "PMID:1"
    assert "Fallback behavior" in response["trace"][0].detail


def test_retrieve_pubmed_fallback_empty_or_failed_never_invents_citations(monkeypatch) -> None:
    pubmed = Mock(source_type=SourceType.PUBMED)
    pubmed.invoke.return_value = _pubmed_result()
    monkeypatch.setattr("research.nodes.ALL_DATA_SOURCE_TOOLS", [pubmed])
    state = {
        "query": "Summarize aspirin evidence with output formatting.",
        "triage_plan": TriagePlan(selected_sources=[], reasoning="no source"),
    }

    empty_response = retrieve(state)
    assert empty_response["retrieved_results"][0].citations == []

    pubmed.invoke.side_effect = RuntimeError("PubMed unavailable")
    failed_response = retrieve(state)
    assert failed_response["retrieved_results"] == []
    assert "Fallback behavior" in failed_response["trace"][0].detail


def test_synthesis_fallback_prompt_has_presentation_rule(monkeypatch) -> None:
    """When the structured attempt yields nothing parseable, the unchanged prose path runs."""
    model = Mock()
    model.invoke.return_value = AIMessage(content="Aspirin has evidence.")
    monkeypatch.setattr("research.nodes.get_model", lambda *a, **k: model)

    result = synthesize({"query": "What is aspirin used for?", "mode": "science", "retrieved_results": []})

    assert result["response_document"] is None
    assert result["synthesis"] == "Aspirin has evidence."
    assert any(t.stage == "SYNTHESIS_FALLBACK" for t in result["trace"])

    system_prompt = model.invoke.call_args.args[0][0]["content"].lower()
    assert "never claim that you can or cannot generate ui or interactive graphics" in system_prompt
    assert "never output cei ui json" in system_prompt
    assert "application handles rendering" in system_prompt


def _sample_document() -> ResponseDocument:
    return ResponseDocument(
        title="Aspirin",
        summary="Aspirin reduces cardiovascular risk in the reviewed evidence.",
        key_metrics=[],
        sections=[
            ResponseSection(
                heading="Findings",
                blocks=[ProseBlock(kind="prose", markdown="Aspirin lowers risk. [PMID:1]")],
            ),
        ],
    )


def test_synthesize_structured_success_grounds_and_renders_document(monkeypatch) -> None:
    doc = _sample_document()
    raw = AIMessage(content="", usage_metadata={"input_tokens": 5, "output_tokens": 7, "total_tokens": 12})
    structured_model = Mock()
    structured_model.invoke.return_value = {"parsed": doc, "raw": raw, "parsing_error": None}
    model = Mock()
    model.with_structured_output.return_value = structured_model
    monkeypatch.setattr("research.nodes.get_model", lambda *a, **k: model)
    monkeypatch.setattr("research.nodes.build_corpus", lambda *texts: " ".join(texts))
    monkeypatch.setattr("research.nodes.ground_document", lambda document, corpus: (document, ["dropped an ungrounded chart"]))
    monkeypatch.setattr(
        "research.nodes.render_document_markdown",
        lambda document, **kw: f"# {document.title}\n\nAspirin lowers risk.\n\nSources: [PMID:1]",
    )

    result = synthesize({"query": "What is aspirin used for?", "mode": "science", "retrieved_results": []})

    assert result["response_document"] is doc
    assert result["synthesis"] == "# Aspirin\n\nAspirin lowers risk.\n\nSources: [PMID:1]"
    stages = [t.stage for t in result["trace"]]
    assert "SYNTHESIS" in stages
    assert "GROUNDING" in stages
    assert "SYNTHESIS_FALLBACK" not in stages
    synthesis_trace = next(t for t in result["trace"] if t.stage == "SYNTHESIS")
    assert synthesis_trace.input_tokens == 5
    assert synthesis_trace.output_tokens == 7


def test_synthesize_falls_back_when_structured_output_is_unparsed(monkeypatch) -> None:
    structured_model = Mock()
    structured_model.invoke.return_value = {"parsed": None, "raw": None, "parsing_error": "no tool call"}
    model = Mock()
    model.with_structured_output.return_value = structured_model
    model.invoke.return_value = AIMessage(content="Aspirin has evidence.")
    monkeypatch.setattr("research.nodes.get_model", lambda *a, **k: model)

    result = synthesize({"query": "What is aspirin used for?", "mode": "science", "retrieved_results": []})

    assert result["response_document"] is None
    assert result["synthesis"] == "Aspirin has evidence."
    assert any(t.stage == "SYNTHESIS_FALLBACK" for t in result["trace"])


def test_cite_resolves_ids_found_in_sources_lines() -> None:
    """`cite` must still work on rendered markdown that carries `Sources: [ID]` lines."""
    synthesis = "# Aspirin\n\nAspirin lowers risk.\n\nSources: [PMID:1]"
    result = cite({
        "synthesis": synthesis,
        "retrieved_results": [_pubmed_result(citations=[_citation()])],
    })

    text = result["messages"][-1].content
    assert "References" in text
    assert "[PMID:1]" in text
    assert result["trace"][0].detail.startswith("resolved=1")

def test_cite_splits_grouped_citation_brackets() -> None:
    second = _citation().model_copy(
        update={"source_id": "PMID:2", "url": "https://pubmed.ncbi.nlm.nih.gov/2/"}
    )
    result = cite({
        "synthesis": "Aspirin lowers risk [PMID:1, PMID:2].",
        "retrieved_results": [_pubmed_result(citations=[_citation(), second])],
    })

    assert result["trace"][0].detail == "resolved=2 | referenced=2 | unresolved=0"
