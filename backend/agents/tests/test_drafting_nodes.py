from __future__ import annotations

from unittest.mock import Mock

from langchain_core.messages import AIMessage, HumanMessage

from drafting.nodes import draft, export_documents, plan_visuals, revise
from drafting.state import DocumentType
from shared.schemas.response_blocks import (
    ChartBlock,
    ChartSeriesItem,
    ProseBlock,
    ResponseDocument,
    ResponseSection,
)


def _sample_document(title: str = "Grant Proposal") -> ResponseDocument:
    return ResponseDocument(
        title=title,
        summary="A concise proposal summary.",
        key_metrics=[],
        sections=[ResponseSection(heading="Aims", blocks=[ProseBlock(kind="prose", markdown="Aim 1.")])],
    )


def _base_draft_state(**overrides) -> dict:
    state = {
        "messages": [HumanMessage(content="Draft a grant about aspirin.")],
        "mode": "science",
        "document_type": DocumentType.GRANT,
        "guidelines": [],
        "research_findings": [],
        "chart_data": {},
    }
    state.update(overrides)
    return state


def test_draft_structured_success_sets_draft_document_and_markdown(monkeypatch) -> None:
    doc = _sample_document()
    raw = AIMessage(content="", usage_metadata={"input_tokens": 10, "output_tokens": 20, "total_tokens": 30})
    structured_model = Mock()
    structured_model.invoke.return_value = {"parsed": doc, "raw": raw, "parsing_error": None}
    model = Mock()
    model.with_structured_output.return_value = structured_model
    monkeypatch.setattr("drafting.nodes.get_model", lambda *a, **k: model)
    monkeypatch.setattr("drafting.nodes.build_corpus", lambda *texts: " ".join(texts))
    monkeypatch.setattr("drafting.nodes.ground_document", lambda d, corpus: (d, ["dropped an ungrounded metric"]))
    monkeypatch.setattr("drafting.nodes.render_document_markdown", lambda d, **kw: f"# {d.title}\n\nAim 1.")

    result = draft(_base_draft_state())

    assert result["draft_document"] is doc
    assert result["draft"] == "# Grant Proposal\n\nAim 1."
    stages = [t.stage for t in result["trace"]]
    assert "DRAFT" in stages
    assert "GROUNDING" in stages
    assert "DRAFT_FALLBACK" not in stages
    draft_trace = next(t for t in result["trace"] if t.stage == "DRAFT")
    assert draft_trace.input_tokens == 10
    assert draft_trace.output_tokens == 20


def test_draft_falls_back_when_structured_output_is_unparsed(monkeypatch) -> None:
    structured_model = Mock()
    structured_model.invoke.return_value = {"parsed": None, "raw": None, "parsing_error": "no tool call"}
    model = Mock()
    model.with_structured_output.return_value = structured_model
    model.invoke.return_value = AIMessage(
        content="```python\nmatplotlib.pyplot.plot([1, 2])\n```\nPlain draft text."
    )
    monkeypatch.setattr("drafting.nodes.get_model", lambda *a, **k: model)

    result = draft(_base_draft_state())

    assert result["draft_document"] is None
    assert "matplotlib" not in result["draft"]
    assert "Plain draft text." in result["draft"]
    assert any(t.stage == "DRAFT_FALLBACK" for t in result["trace"])


def test_revise_structured_success_sets_draft_document(monkeypatch) -> None:
    doc = _sample_document(title="Revised Grant Proposal")
    raw = AIMessage(content="", usage_metadata={"input_tokens": 4, "output_tokens": 6, "total_tokens": 10})
    structured_model = Mock()
    structured_model.invoke.return_value = {"parsed": doc, "raw": raw, "parsing_error": None}
    model = Mock()
    model.with_structured_output.return_value = structured_model
    monkeypatch.setattr("drafting.nodes.get_model", lambda *a, **k: model)
    monkeypatch.setattr("drafting.nodes.build_corpus", lambda *texts: " ".join(texts))
    monkeypatch.setattr("drafting.nodes.ground_document", lambda d, corpus: (d, []))
    monkeypatch.setattr("drafting.nodes.render_document_markdown", lambda d, **kw: f"# {d.title}\n\nAim 1.")

    state = _base_draft_state(draft="# Grant Proposal\n\nAim 1.", revision_feedback="Add a budget section.")
    result = revise(state)

    assert result["draft_document"] is doc
    assert result["draft"] == "# Revised Grant Proposal\n\nAim 1."
    assert result["revision_feedback"] is None
    assert any(t.stage == "REVISE" for t in result["trace"])


def test_plan_visuals_builds_specs_from_document_without_llm_call(monkeypatch) -> None:
    doc = ResponseDocument(
        title="Report",
        summary="Summary.",
        key_metrics=[],
        sections=[
            ResponseSection(
                heading="Trial Results",
                blocks=[
                    ChartBlock(
                        kind="chart",
                        title="Response Rate",
                        chart_type="bar-horizontal",
                        categories=["Drug A", "Drug B"],
                        series=[ChartSeriesItem(name="ORR", values=[42.0, 31.0])],
                        x_label="Arm",
                        y_label="ORR (%)",
                        source_ids=["NCT:1"],
                    ),
                ],
            ),
        ],
    )

    def _boom(*_a, **_k):
        raise AssertionError("plan_visuals must not call the LLM when a document exists")

    monkeypatch.setattr("drafting.nodes.get_model", _boom)

    # export_formats is text-only; the LLM-detection gate used to skip everything on this — specs
    # must still be computed because the UI needs them regardless of export format.
    result = plan_visuals({"draft_document": doc, "export_formats": ["txt"]})

    assert result["chart_specs"] == [{
        "title": "Response Rate",
        "chart_type": "bar",  # mapped from bar-horizontal — ChartTool has no horizontal-bar renderer
        "x_label": "Arm",
        "y_label": "ORR (%)",
        "section": "Trial Results",
        "data": {"x": ["Drug A", "Drug B"], "ORR": [42.0, 31.0]},
    }]
    assert result["trace"][0].stage == "PLAN_VISUALS"


class _FakeChartTool:
    def __init__(self, _ci) -> None:
        pass

    def render(self, _spec) -> bytes:
        return b"fake-png-bytes"


def _patch_chart_rendering(monkeypatch) -> None:
    monkeypatch.setattr("drafting.nodes.ChartTool", _FakeChartTool)
    monkeypatch.setattr("drafting.nodes.CodeInterpreterTool", lambda **_kw: object())


def test_export_documents_text_has_no_base64_when_document_exists(monkeypatch) -> None:
    doc = _sample_document(title="Report")
    monkeypatch.setattr(
        "drafting.nodes.document_sections_for_export",
        lambda _d: [{"heading": "Overview", "content": "Revenue grew.", "order": 0}],
    )
    monkeypatch.setattr(
        "drafting.nodes.render_document_markdown",
        lambda d, **kw: f"# {d.title}\n\nRevenue grew.\n\n| Year | USD |\n|---|---|\n| 2023 | 1 |\n",
    )
    _patch_chart_rendering(monkeypatch)

    state = {
        "export_formats": ["md"],
        "draft_document": doc,
        "draft": "",
        "chart_specs": [{
            "title": "Revenue",
            "chart_type": "bar",
            "x_label": "",
            "y_label": "",
            "section": "Overview",
            "data": {"x": ["2023", "2024"], "USD": [1.0, 2.0]},
        }],
        "document_type": DocumentType.WHITEPAPER,
    }

    result = export_documents(state)

    text = result["messages"][-1].content
    assert "data:image/png;base64" not in text
    assert result["exports"]
    assert result["exports"][0]["format"] == "md"


def test_export_documents_legacy_path_has_no_base64_in_text(monkeypatch) -> None:
    _patch_chart_rendering(monkeypatch)

    draft_text = "# Report\n\n## Overview\n\nRevenue grew steadily.\n"
    state = {
        "export_formats": ["md"],
        "draft_document": None,
        "draft": draft_text,
        "chart_specs": [{
            "title": "Revenue",
            "chart_type": "bar",
            "x_label": "",
            "y_label": "",
            "section": "Overview",
            "data": {"x": ["2023", "2024"], "USD": [1.0, 2.0]},
        }],
        "document_type": DocumentType.WHITEPAPER,
    }

    result = export_documents(state)

    text = result["messages"][-1].content
    assert "data:image/png;base64" not in text
    assert "*Figure: Revenue*" in text
