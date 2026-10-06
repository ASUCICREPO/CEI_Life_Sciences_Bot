from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import Mock

from langchain_core.messages import AIMessage, HumanMessage

from analysis.graph import compile_graph
from analysis.nodes import analyze, compare, confirm_scope, gather, report, scope
from analysis.state import AnalysisType, Comparison, ComparisonsOutput, GatherQueries, ScopeProposal
from shared.schemas.citation import Citation, SourceType
from shared.schemas.response_blocks import ProseBlock, ResponseDocument, ResponseSection
from shared.tools.base import DataSourceResult
from shared.tools.hitl.inline_functions import build_scope_confirmation_surface


def _citation(source_id: str = "PMID:1") -> Citation:
    return Citation(
        source_id=source_id,
        source_type=SourceType.PUBMED,
        url="https://pubmed.ncbi.nlm.nih.gov/1/",
        title="Evidence",
        authors=["Researcher"],
        year=2026,
        relevant_passage="Evidence passage.",
        retrieved_at=datetime.now(UTC),
    )


def _result(source_type: SourceType = SourceType.PUBMED) -> DataSourceResult:
    return DataSourceResult(
        source_type=source_type,
        results=[{"title": "Record"}],
        citations=[_citation()],
    )


# ── scope-confirmation surface ──────────────────────────────────────────────


def test_scope_confirmation_surface_is_valid_cei_ui() -> None:
    surface = build_scope_confirmation_surface(
        surface_id="analysis-scope",
        revision=1,
        analysis_type="competitor",
        competitors=["Acme Bio"],
        market="Oncology",
        jurisdiction="US",
        focal_entity="Our Co",
    )
    assert surface["surface_id"] == "analysis-scope"
    assert surface["revision"] == 1
    component_types = {c["type"] for c in surface["components"]}
    assert "ReviewGate" in component_types
    assert "TextField" in component_types


# ── graph pause / resume behavior ───────────────────────────────────────────


def _mock_scope_model(monkeypatch) -> Mock:
    raw = AIMessage(content="", usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})
    structured_model = Mock()
    structured_model.invoke.return_value = {
        "parsed": ScopeProposal(
            analysis_type=AnalysisType.COMPETITOR,
            competitors=["Acme Bio"],
            market="Oncology",
            jurisdiction="US",
            focal_entity="Our Co",
        ),
        "raw": raw,
    }
    model = Mock()
    model.with_structured_output.return_value = structured_model
    monkeypatch.setattr("analysis.nodes.get_model", lambda *a, **k: model)
    return model


def test_graph_pauses_at_confirm_scope_before_any_tool_call(monkeypatch) -> None:
    _mock_scope_model(monkeypatch)
    tool = Mock(source_type=SourceType.PUBMED)
    monkeypatch.setattr("analysis.nodes.ALL_DATA_SOURCE_TOOLS", [tool])

    from langgraph.checkpoint.memory import InMemorySaver
    graph = compile_graph(checkpointer=InMemorySaver())

    import asyncio

    config = {"configurable": {"thread_id": "s1"}}
    result = asyncio.run(graph.ainvoke(_base_initial_state(), config=config))

    assert result.get("__interrupt__")
    tool.invoke.assert_not_called()


def test_confirm_scope_skips_interrupt_when_already_confirmed() -> None:
    state = {
        "analysis_type": AnalysisType.COMPETITOR,
        "confirmed_scope": True,
    }
    assert confirm_scope(state) == {}


def _base_initial_state() -> dict:
    return {
        "messages": [HumanMessage(content="Analyze competitors to Our Co in oncology")],
        "mode": "science",
        "user_id": "u1",
        "session_id": "s1",
        "query": "Analyze competitors to Our Co in oncology",
        "analysis_type": None,
        "competitors": [],
        "market": None,
        "jurisdiction": None,
        "focal_entity": None,
        "confirmed_scope": False,
        "scope_revision": 0,
        "scope_correction": None,
        "retrieved_results": [],
        "findings": [],
        "comparisons": [],
        "report": "",
        "response_document": None,
        "trace": [],
    }


def _review_gate_action(action_id: str) -> dict:
    return {"action_id": action_id, "interaction_id": "analysis-scope-review", "component_id": "scope-review", "value": None}


def _edit_action(text: str) -> dict:
    return {"action_id": "edit-scope", "interaction_id": "analysis-scope-edit", "component_id": "scope-edit", "value": text}


def test_graph_reject_ends_without_tool_call(monkeypatch) -> None:
    import asyncio
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command

    _mock_scope_model(monkeypatch)
    tool = Mock(source_type=SourceType.PUBMED)
    monkeypatch.setattr("analysis.nodes.ALL_DATA_SOURCE_TOOLS", [tool])

    graph = compile_graph(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "s1"}}
    first = asyncio.run(graph.ainvoke(_base_initial_state(), config=config))
    assert first.get("__interrupt__")

    final = asyncio.run(graph.ainvoke(Command(resume=_review_gate_action("reject-scope")), config=config))
    assert not final.get("__interrupt__")
    tool.invoke.assert_not_called()
    assert "cancelled" in final["messages"][-1].content.lower()


def test_graph_edit_reproposes_with_incremented_revision(monkeypatch) -> None:
    import asyncio
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command

    _mock_scope_model(monkeypatch)
    tool = Mock(source_type=SourceType.PUBMED)
    monkeypatch.setattr("analysis.nodes.ALL_DATA_SOURCE_TOOLS", [tool])

    graph = compile_graph(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "s2"}}
    first = asyncio.run(graph.ainvoke(_base_initial_state(), config=config))
    first_surface = first["__interrupt__"][0].value
    assert first_surface["revision"] == 1

    second = asyncio.run(graph.ainvoke(Command(resume=_edit_action("Also include Beta Bio")), config=config))
    assert second.get("__interrupt__")
    second_surface = second["__interrupt__"][0].value
    assert second_surface["surface_id"] == "analysis-scope"
    assert second_surface["revision"] == 2
    tool.invoke.assert_not_called()


def test_graph_approve_runs_through_to_report(monkeypatch) -> None:
    import asyncio
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command

    scope_proposal = ScopeProposal(
        analysis_type=AnalysisType.COMPETITOR, competitors=["Acme Bio"], market="Oncology",
        jurisdiction="US", focal_entity="Our Co",
    )
    gather_queries = GatherQueries(source_queries={"pubmed": "oncology competitors"})
    comparisons_output = ComparisonsOutput(
        comparisons=[Comparison(dimension="Modality / mechanism", findings=["Small molecule", "Biologic"], sources=[])]
    )
    document = _sample_document()

    def _with_structured_output(schema, include_raw=True):
        structured = Mock()
        if schema is ScopeProposal:
            structured.invoke.return_value = {"parsed": scope_proposal, "raw": AIMessage(content="")}
        elif schema is GatherQueries:
            structured.invoke.return_value = {"parsed": gather_queries, "raw": AIMessage(content="")}
        elif schema is ComparisonsOutput:
            structured.invoke.return_value = {"parsed": comparisons_output, "raw": None, "parsing_error": None}
        else:
            structured.invoke.return_value = {"parsed": document, "raw": None, "parsing_error": None}
        return structured

    model = Mock()
    model.with_structured_output.side_effect = _with_structured_output
    monkeypatch.setattr("analysis.nodes.get_model", lambda *a, **k: model)

    pubmed_tool = Mock(source_type=SourceType.PUBMED)
    pubmed_tool.invoke.return_value = _result(SourceType.PUBMED)
    monkeypatch.setattr("analysis.nodes.ALL_DATA_SOURCE_TOOLS", [pubmed_tool])

    graph = compile_graph(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "s3"}}
    first = asyncio.run(graph.ainvoke(_base_initial_state(), config=config))
    assert first.get("__interrupt__")

    final = asyncio.run(graph.ainvoke(Command(resume=_review_gate_action("approve-scope")), config=config))
    assert not final.get("__interrupt__")
    pubmed_tool.invoke.assert_called_once_with(query="oncology competitors")
    assert final["response_document"].title == document.title


# ── gather ───────────────────────────────────────────────────────────────────


def test_gather_calls_fixed_source_map_only(monkeypatch) -> None:
    raw = AIMessage(content="")
    structured_model = Mock()
    structured_model.invoke.return_value = {"parsed": GatherQueries(source_queries={"pubmed": "oncology trials"}), "raw": raw}
    model = Mock()
    model.with_structured_output.return_value = structured_model
    monkeypatch.setattr("analysis.nodes.get_model", lambda *a, **k: model)

    pubmed_tool = Mock(source_type=SourceType.PUBMED)
    pubmed_tool.invoke.return_value = _result(SourceType.PUBMED)
    monkeypatch.setattr("analysis.nodes.ALL_DATA_SOURCE_TOOLS", [pubmed_tool])

    state = {
        "analysis_type": AnalysisType.LEGAL_LANDSCAPE,
        "query": "legal landscape for Acme",
        "competitors": [],
        "market": None,
        "jurisdiction": "US",
        "focal_entity": "Acme",
    }
    result = gather(state)

    assert len(result["retrieved_results"]) == 1
    pubmed_tool.invoke.assert_called_once_with(query="oncology trials")


# ── analyze ──────────────────────────────────────────────────────────────────


def test_analyze_builds_one_finding_per_source() -> None:
    state = {"retrieved_results": [_result(SourceType.PUBMED), _result(SourceType.OPENFDA)]}
    result = analyze(state)
    assert len(result["findings"]) == 2
    assert "pubmed" in result["findings"][0]
    assert "openfda" in result["findings"][1]


# ── compare: one test per framework ────────────────────────────────────────


def _mock_compare_model(monkeypatch, comparisons: list[Comparison]) -> None:
    structured_model = Mock()
    structured_model.invoke.return_value = {
        "parsed": ComparisonsOutput(comparisons=comparisons),
        "raw": None,
        "parsing_error": None,
    }
    model = Mock()
    model.with_structured_output.return_value = structured_model
    monkeypatch.setattr("analysis.nodes.get_model", lambda *a, **k: model)


def test_compare_competitor_framework(monkeypatch) -> None:
    _mock_compare_model(monkeypatch, [
        Comparison(dimension="Modality / mechanism", findings=["Small molecule", "Biologic"], sources=["PMID:1"]),
    ])
    state = {
        "analysis_type": AnalysisType.COMPETITOR,
        "mode": "science",
        "competitors": ["Acme Bio"],
        "focal_entity": "Our Co",
        "findings": ["pubmed: 1 record(s) retrieved. Sources: PMID:1."],
    }
    result = compare(state)
    dims = [c.dimension for c in result["comparisons"]]
    assert "Modality / mechanism" in dims
    assert len(dims) == 7
    for comparison in result["comparisons"]:
        assert len(comparison.findings) == 2  # focal_entity + competitor


def test_compare_market_framework(monkeypatch) -> None:
    _mock_compare_model(monkeypatch, [
        Comparison(dimension="Patient population", findings=["~50,000 patients in the US"], sources=["PMID:1"]),
    ])
    state = {
        "analysis_type": AnalysisType.MARKET,
        "mode": "science",
        "competitors": [],
        "market": "Oncology",
        "focal_entity": None,
        "findings": [],
    }
    result = compare(state)
    dims = [c.dimension for c in result["comparisons"]]
    assert dims == [
        "Patient population", "Standard of care", "Reimbursement / coverage",
        "Pipeline density", "Key players", "Drivers and barriers",
    ]
    for comparison in result["comparisons"]:
        assert len(comparison.findings) == 1


def test_compare_legal_landscape_framework(monkeypatch) -> None:
    _mock_compare_model(monkeypatch, [
        Comparison(dimension="Approvals and labels", findings=["Approved in the US"], sources=["OPENFDA:1"]),
    ])
    state = {
        "analysis_type": AnalysisType.LEGAL_LANDSCAPE,
        "mode": "business",
        "competitors": [],
        "jurisdiction": "US",
        "focal_entity": "Acme",
        "findings": [],
    }
    result = compare(state)
    dims = [c.dimension for c in result["comparisons"]]
    assert dims == [
        "Regulatory pathway and designations", "Approvals and labels",
        "Patent landscape and expiries", "Coverage and reimbursement", "Open risks",
    ]


def test_compare_competitor_business_mode_adds_swot(monkeypatch) -> None:
    _mock_compare_model(monkeypatch, [])
    state = {
        "analysis_type": AnalysisType.COMPETITOR,
        "mode": "business",
        "competitors": ["Acme Bio"],
        "focal_entity": "Our Co",
        "findings": [],
    }
    result = compare(state)
    dims = [c.dimension for c in result["comparisons"]]
    assert any(d.startswith("SWOT") for d in dims)


# ── report ───────────────────────────────────────────────────────────────────


def _sample_document() -> ResponseDocument:
    return ResponseDocument(
        title="Competitor analysis",
        summary="Our Co leads in differentiation.",
        key_metrics=[],
        sections=[
            ResponseSection(
                heading="Findings",
                blocks=[ProseBlock(kind="prose", markdown="Our Co leads. [PMID:1]")],
            ),
        ],
    )


def test_report_injects_matrix_when_missing_and_grounds(monkeypatch) -> None:
    doc = _sample_document()
    structured_model = Mock()
    structured_model.invoke.return_value = {"parsed": doc, "raw": None, "parsing_error": None}
    model = Mock()
    model.with_structured_output.return_value = structured_model
    monkeypatch.setattr("analysis.nodes.get_model", lambda *a, **k: model)
    monkeypatch.setattr("analysis.nodes.build_corpus", lambda *texts: " ".join(texts))
    monkeypatch.setattr("analysis.nodes.ground_document", lambda document, corpus: (document, []))
    monkeypatch.setattr("analysis.nodes.render_document_markdown", lambda document, **kw: "# report")

    state = {
        "mode": "science",
        "analysis_type": AnalysisType.COMPETITOR,
        "query": "Analyze Our Co vs Acme Bio",
        "retrieved_results": [],
        "comparisons": [
            Comparison(dimension="Modality / mechanism", findings=["Small molecule", "Biologic"], sources=["PMID:1"]),
        ],
        "competitors": ["Acme Bio"],
        "focal_entity": "Our Co",
    }
    result = report(state)

    assert result["response_document"] is doc
    kinds = {block.kind for section in doc.sections for block in section.blocks}
    assert "comparison" in kinds or "table" in kinds
    assert any(t.stage == "REPORT" for t in result["trace"])
