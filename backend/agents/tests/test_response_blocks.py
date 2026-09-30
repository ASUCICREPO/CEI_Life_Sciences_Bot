from __future__ import annotations

import json

from shared.presenters.markdown import document_sections_for_export, render_document_markdown
from shared.schemas.response_blocks import (
    ArtifactBlock,
    CalloutBlock,
    ChartBlock,
    ChartSeriesItem,
    DataTable,
    MetricItem,
    MetricsBlock,
    ProseBlock,
    ResponseDocument,
    ResponseSection,
    TableBlock,
)


def test_response_document_schema_has_no_discriminator_and_hides_server_fields() -> None:
    schema_text = json.dumps(ResponseDocument.model_json_schema())

    assert "discriminator" not in schema_text
    assert "verification" not in schema_text
    assert "unverified_cells" not in schema_text


def _sample_document() -> ResponseDocument:
    return ResponseDocument(
        title="GLP-1 agonists",
        summary="Semaglutide gives the largest weight loss [PMID:111].",
        key_metrics=[
            MetricItem(label="Mean weight loss", value=14.9, unit="%", basis="sourced", source_ids=["PMID:111"])
        ],
        sections=[
            ResponseSection(
                heading="Efficacy",
                blocks=[
                    ProseBlock(kind="prose", markdown="Semaglutide outperforms liraglutide [PMID:111]."),
                    TableBlock(
                        kind="table",
                        columns=["Agent", "Weight loss (%)"],
                        rows=[["Semaglutide", 14.9], ["Liraglutide", 8.0]],
                        source_ids=["PMID:111"],
                        unverified_cells=[(1, 1)],
                    ),
                    MetricsBlock(
                        kind="metrics",
                        metrics=[
                            MetricItem(label="Weight loss", value=14.9, unit="%", source_ids=["PMID:111"])
                        ],
                    ),
                    ChartBlock(
                        kind="chart",
                        title="Weight loss by agent",
                        categories=["Semaglutide", "Liraglutide"],
                        series=[ChartSeriesItem(name="Weight loss", values=[14.9, 8.0])],
                        source_ids=["PMID:111"],
                    ),
                    CalloutBlock(kind="callout", tone="info", markdown="Two trials only [PMID:111]."),
                    ArtifactBlock(
                        kind="artifact",
                        title="Dose explorer",
                        purpose="Show dose response.",
                        html="<div>hi</div><script>1</script>",
                        data=DataTable(columns=["Dose", "Loss"], rows=[["1.0 mg", 6.0]]),
                        source_ids=["PMID:111"],
                    ),
                ],
            )
        ],
        confidence="medium",
        confidence_rationale="Two trials; head-to-head data are limited.",
        open_questions=["What is the long-term durability?"],
    )


def test_render_document_markdown_keeps_bracketed_source_ids_for_citation_regex() -> None:
    text = render_document_markdown(_sample_document())

    assert "[PMID:111]" in text
    assert "Sources: [PMID:111]" in text


def test_render_document_markdown_never_includes_artifact_html() -> None:
    text = render_document_markdown(_sample_document())

    assert "<div>hi</div>" not in text
    assert "<script>" not in text
    assert "Interactive view: Dose explorer" in text


def test_render_document_markdown_chart_table_vs_omit_mode() -> None:
    doc = _sample_document()

    with_table = render_document_markdown(doc, chart_mode="table")
    omitted = render_document_markdown(doc, chart_mode="omit")

    assert "Semaglutide" in with_table.split("*Figure: Weight loss by agent*")[1]
    assert "*Figure: Weight loss by agent*" not in omitted


def test_render_document_markdown_flags_unverified_table_cell() -> None:
    text = render_document_markdown(_sample_document())

    assert "8 †" in text or "8.0 †" in text or "† " in text


def test_document_sections_for_export_returns_ordered_dicts() -> None:
    doc = _sample_document()
    doc = doc.model_copy(
        update={
            "sections": [
                *doc.sections,
                ResponseSection(heading="Limitations", blocks=[ProseBlock(kind="prose", markdown="Small n.")]),
            ]
        }
    )

    sections = document_sections_for_export(doc)

    assert [s["heading"] for s in sections] == ["Efficacy", "Limitations"]
    assert [s["order"] for s in sections] == [0, 1]
    assert all(isinstance(s["content"], str) for s in sections)
    assert "*Figure:" not in sections[0]["content"]
