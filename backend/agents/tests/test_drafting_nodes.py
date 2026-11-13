from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import Mock

from langchain_core.messages import AIMessage, HumanMessage

from drafting.nodes import (
    GuidelinePlan,
    GuidelinesExtraction,
    draft,
    export_documents,
    plan_visuals,
    research_guidelines,
    revise,
)
from drafting.state import DocumentType
from shared.schemas.citation import Citation, SourceType
from shared.schemas.response_blocks import (
    ChartBlock,
    ChartSeriesItem,
    ProseBlock,
    ResponseDocument,
    ResponseSection,
)
from shared.tools.base import DataSourceResult


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


def test_export_documents_text_has_no_base64_when_document_exists(monkeypatch) -> None:
    """File generation is back (858bb1a reverted, product decision): export_documents renders
    files for the requested formats; with no export storage configured (local dev), the files
    are generated but not persisted, and a note says so."""
    from shared.tools.export import ExportArtifact

    doc = _sample_document(title="Report")
    monkeypatch.setattr(
        "drafting.nodes.document_sections_for_export",
        lambda _d: [{"heading": "Overview", "content": "Revenue grew.", "order": 0}],
    )
    monkeypatch.setattr(
        "drafting.nodes.render_document_markdown",
        lambda d, **kw: f"# {d.title}\n\nRevenue grew.\n\n| Year | USD |\n|---|---|\n| 2023 | 1 |\n",
    )
    monkeypatch.setattr("drafting.nodes.ChartTool", lambda _ci: Mock(render=Mock(return_value=None)))

    generated_formats = []

    def fake_generate(self, *, title, sections, charts, formats, output_dir, chart_sections=None):
        generated_formats.append(list(formats))
        path = output_dir / "report.md"
        path.write_text("# Report\n\nRevenue grew.\n", encoding="utf-8")
        return [ExportArtifact(format="md", filename=path.name, local_path=str(path), byte_size=path.stat().st_size)]

    monkeypatch.setattr("shared.tools.export.ExportTool.generate", fake_generate)
    monkeypatch.setattr("drafting.nodes._export_repository", lambda: None)

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
    assert generated_formats == [["md"]]
    assert result["exports"] == []
    assert "not configured" in (result["chart_instructions"] or "")


def test_export_documents_persists_exports_with_moto(monkeypatch) -> None:
    """Exports persist via `ExportRepository` when export storage is configured (moto)."""
    import boto3
    from moto import mock_aws
    from shared.config import config
    from shared.persistence.exports import _EXPORT_ID
    from shared.tools.export import ExportArtifact

    doc = _sample_document(title="Report")
    monkeypatch.setattr(
        "drafting.nodes.document_sections_for_export",
        lambda _d: [{"heading": "Overview", "content": "Revenue grew.", "order": 0}],
    )
    monkeypatch.setattr("drafting.nodes.render_document_markdown", lambda d, **kw: f"# {d.title}\n\nRevenue grew.\n")

    def fake_generate(self, *, title, sections, charts, formats, output_dir, chart_sections=None):
        path = output_dir / "report.md"
        path.write_text("# Report\n\nRevenue grew.\n", encoding="utf-8")
        return [ExportArtifact(format="md", filename=path.name, local_path=str(path), byte_size=path.stat().st_size)]

    monkeypatch.setattr("shared.tools.export.ExportTool.generate", fake_generate)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")

    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="exports-table", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}, {"AttributeName": "export_id", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"}, {"AttributeName": "export_id", "AttributeType": "S"}],
        )
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="exports-bucket")
        monkeypatch.setattr(config, "export_metadata_table", "exports-table")
        monkeypatch.setattr(config, "export_bucket", "exports-bucket")

        state = {
            "export_formats": ["md"],
            "draft_document": doc,
            "draft": "",
            "chart_specs": [],
            "document_type": DocumentType.WHITEPAPER,
            "user_id": "alice",
            "session_id": "6f1c2d3e-0000-4000-8000-000000000001",
            "export_turn_id": "turn-1",
        }

        result = export_documents(state)

    assert len(result["exports"]) == 1
    export_item = result["exports"][0]
    assert export_item["format"] == "md"
    assert export_item["filename"] == "report.md"
    assert _EXPORT_ID.fullmatch(export_item["export_id"])


def test_export_documents_legacy_path_has_no_base64_in_text() -> None:
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


# ── research_guidelines: B110 attachments wiring ────────────────────────────


def _attachment_result() -> DataSourceResult:
    return DataSourceResult(
        source_type=SourceType.ATTACHMENTS,
        results=[{"file_id": "f1", "filename": "notes.pdf", "text": "The chunk text.", "score": 0.5}],
        citations=[Citation(
            source_id="attachment:f1:0",
            source_type=SourceType.ATTACHMENTS,
            url="",
            title="notes.pdf",
            relevant_passage="The chunk text.",
            retrieved_at=datetime.now(UTC),
        )],
    )


def _guideline_models(monkeypatch, parsed_extraction: GuidelinesExtraction) -> None:
    raw = AIMessage(content="", usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})

    def _with_structured_output(schema, include_raw=True):
        structured = Mock()
        if schema is GuidelinePlan:
            structured.invoke.return_value = {
                "parsed": GuidelinePlan(guideline_focus="formatting", reasoning="test"), "raw": raw,
            }
        else:
            structured.invoke.return_value = {"parsed": parsed_extraction, "raw": raw}
        return structured

    model = Mock()
    model.with_structured_output.side_effect = _with_structured_output
    monkeypatch.setattr("drafting.nodes.get_model", lambda *a, **k: model)


def test_research_guidelines_appends_attachments_result_to_context(monkeypatch) -> None:
    monkeypatch.setattr("drafting.nodes.ALL_DATA_SOURCE_TOOLS", [])
    captured = {}

    def fake_retrieve_attachments(query, *, user_id, session_id):
        captured["user_id"] = user_id
        captured["session_id"] = session_id
        return _attachment_result()

    monkeypatch.setattr("drafting.nodes.retrieve_attachments", fake_retrieve_attachments)
    _guideline_models(monkeypatch, GuidelinesExtraction(guidelines=[]))

    state = {
        "messages": [HumanMessage(content="Draft a grant about aspirin.")],
        "document_type": DocumentType.GRANT,
        "long_term_context": [],
        "user_id": "u1",
        "session_id": "s1",
    }
    result = research_guidelines(state)

    assert captured == {"user_id": "u1", "session_id": "s1"}
    assert any("attachments | results=1" in t.detail for t in result["trace"])


def test_research_guidelines_handles_no_attachments(monkeypatch) -> None:
    monkeypatch.setattr("drafting.nodes.ALL_DATA_SOURCE_TOOLS", [])
    monkeypatch.setattr("drafting.nodes.retrieve_attachments", lambda *a, **k: None)
    _guideline_models(monkeypatch, GuidelinesExtraction(guidelines=[]))

    state = {
        "messages": [HumanMessage(content="Draft a grant about aspirin.")],
        "document_type": DocumentType.GRANT,
        "long_term_context": [],
    }
    result = research_guidelines(state)

    assert any("attachments | results=0" in t.detail for t in result["trace"])
