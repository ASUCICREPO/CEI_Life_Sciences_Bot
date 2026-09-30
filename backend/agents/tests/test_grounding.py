from __future__ import annotations

from shared.grounding import build_corpus, ground_document, is_grounded
from shared.schemas.response_blocks import (
    CalloutBlock,
    ChartBlock,
    ChartSeriesItem,
    DataTable,
    MetricItem,
    MetricsBlock,
    ArtifactBlock,
    ResponseDocument,
    ResponseSection,
    TableBlock,
)


def test_build_corpus_normalizes_case_separators_and_whitespace() -> None:
    corpus = build_corpus("Enrolled  1,204   Patients.\n", "Weight loss was  −5.2% .")

    assert corpus == "enrolled 1204 patients. weight loss was -5.2% ."


def test_single_digit_value_is_not_grounded_inside_a_year() -> None:
    assert is_grounded(1, "the trial enrolled patients in 2019") is False


def test_thousands_separated_value_is_grounded() -> None:
    corpus = build_corpus("Enrolled 1,204 patients across two sites.")

    assert is_grounded(1204, corpus) is True


def test_one_decimal_value_is_grounded() -> None:
    corpus = build_corpus("Mean weight loss was 14.9% at week 68.")

    assert is_grounded(14.9, corpus) is True


def _document(sections: list[ResponseSection], key_metrics: list[MetricItem] | None = None) -> ResponseDocument:
    return ResponseDocument(
        title="Doc",
        summary="Summary",
        key_metrics=key_metrics or [],
        sections=sections,
    )


def test_ground_document_drops_sourced_chart_with_ungrounded_values() -> None:
    corpus = build_corpus("Semaglutide gave 14.9% weight loss.")
    doc = _document(
        [
            ResponseSection(
                heading="Efficacy",
                blocks=[
                    ChartBlock(
                        kind="chart",
                        title="Weight loss",
                        categories=["Semaglutide", "Liraglutide"],
                        series=[ChartSeriesItem(name="Weight loss", values=[14.9, 8.0])],
                        basis="sourced",
                        source_ids=["S1"],
                    ),
                    CalloutBlock(kind="callout", tone="info", markdown="Context [S1]"),
                ],
            )
        ]
    )

    grounded, notes = ground_document(doc, corpus)

    kinds = [block.kind for block in grounded.sections[0].blocks]
    assert "chart" not in kinds
    assert any("dropped chart" in note for note in notes)


def test_ground_document_keeps_estimate_chart_and_marks_it() -> None:
    corpus = build_corpus("No numbers here at all.")
    doc = _document(
        [
            ResponseSection(
                heading="Outlook",
                blocks=[
                    ChartBlock(
                        kind="chart",
                        title="Projected demand",
                        categories=["2027", "2028"],
                        series=[ChartSeriesItem(name="Units", values=[100, 200])],
                        basis="estimate",
                        source_ids=[],
                    )
                ],
            )
        ]
    )

    grounded, _ = ground_document(doc, corpus)

    chart = grounded.sections[0].blocks[0]
    assert chart.verification == "estimate"


def test_ground_document_flags_unverified_table_cells() -> None:
    corpus = build_corpus("Semaglutide gave 14.9% weight loss.")
    doc = _document(
        [
            ResponseSection(
                heading="Efficacy",
                blocks=[
                    TableBlock(
                        kind="table",
                        columns=["Agent", "Weight loss (%)"],
                        rows=[["Semaglutide", 14.9], ["Liraglutide", 8.0]],
                        basis="sourced",
                        source_ids=["S1"],
                    )
                ],
            )
        ]
    )

    grounded, notes = ground_document(doc, corpus)

    table = grounded.sections[0].blocks[0]
    assert table.unverified_cells == [(1, 1)]
    assert table.verification == "unverified"
    assert any("unverified cell" in note for note in notes)


def test_ground_document_marks_ungrounded_metric_unverified_and_keeps_it_in_block() -> None:
    corpus = build_corpus("Semaglutide gave 14.9% weight loss.")
    doc = _document(
        [
            ResponseSection(
                heading="Efficacy",
                blocks=[
                    MetricsBlock(
                        kind="metrics",
                        metrics=[
                            MetricItem(label="Weight loss", value=14.9, basis="sourced", source_ids=["S1"]),
                            MetricItem(label="Participants", value=1961, basis="sourced", source_ids=["S1"]),
                        ],
                    )
                ],
            )
        ]
    )

    grounded, notes = ground_document(doc, corpus)

    metrics = grounded.sections[0].blocks[0].metrics
    assert metrics[0].verification == "verified"
    assert metrics[1].verification == "unverified"
    assert len(metrics) == 2
    assert any("marked unverified" in note for note in notes)


def test_ground_document_removes_ungrounded_key_metrics() -> None:
    corpus = build_corpus("Semaglutide gave 14.9% weight loss.")
    doc = _document(
        [ResponseSection(heading="Efficacy", blocks=[CalloutBlock(kind="callout", markdown="Info [S1]")])],
        key_metrics=[
            MetricItem(label="Weight loss", value=14.9, basis="sourced", source_ids=["S1"]),
            MetricItem(label="Participants", value=1961, basis="sourced", source_ids=["S1"]),
        ],
    )

    grounded, notes = ground_document(doc, corpus)

    assert [metric.label for metric in grounded.key_metrics] == ["Weight loss"]
    assert any("removed key metric" in note for note in notes)


def test_ground_document_flags_artifact_data_cells() -> None:
    corpus = build_corpus("Dose of 2.4 mg gave the largest effect.")
    doc = _document(
        [
            ResponseSection(
                heading="Explorer",
                blocks=[
                    ArtifactBlock(
                        kind="artifact",
                        title="Dose explorer",
                        purpose="Show dose response.",
                        html="<div id='out'></div>",
                        data=DataTable(columns=["Dose", "Response"], rows=[["1.0 mg", 6.0], ["2.4 mg", 14.9]]),
                        basis="sourced",
                        source_ids=["S1"],
                    )
                ],
            )
        ]
    )

    grounded, notes = ground_document(doc, corpus)

    artifact = grounded.sections[0].blocks[0]
    assert (0, 1) in artifact.unverified_cells
    assert artifact.verification == "unverified"
    assert any("data cell" in note for note in notes)


def test_ground_document_replaces_all_removed_sections_with_a_callout() -> None:
    corpus = build_corpus("Nothing relevant is mentioned here.")
    doc = _document(
        [
            ResponseSection(
                heading="Efficacy",
                blocks=[
                    ChartBlock(
                        kind="chart",
                        title="Weight loss",
                        categories=["A"],
                        series=[ChartSeriesItem(name="Weight loss", values=[14.9])],
                        basis="sourced",
                        source_ids=["S1"],
                    )
                ],
            )
        ]
    )

    grounded, notes = ground_document(doc, corpus)

    assert len(grounded.sections) == 1
    assert grounded.sections[0].blocks[0].kind == "callout"
    assert any("removed" in note for note in notes)
