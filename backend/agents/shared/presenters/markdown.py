"""Render structured response documents to markdown for text, history, and exports."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from shared.schemas.response_blocks import (
    ArtifactBlock,
    Block,
    CalloutBlock,
    ChartBlock,
    ComparisonBlock,
    DataTable,
    MetricItem,
    MetricsBlock,
    ProseBlock,
    ResponseDocument,
    ResponseSection,
    TableBlock,
    TimelineBlock,
)

ChartMode = Literal["table", "omit"]


def _format_number(value: float) -> str:
    if float(value) == int(value):
        return str(int(value))
    return f"{value:g}"


def _format_cell(value: object) -> str:
    if isinstance(value, bool) or value is None:
        return "" if value is None else str(value)
    if isinstance(value, (int, float)):
        return _format_number(value)
    return str(value)


def _escape_cell(value: object) -> str:
    text = value if isinstance(value, str) else _format_cell(value)
    return text.replace("\n", " ").replace("|", "\\|")


def _source_line(source_ids: Sequence[str]) -> list[str]:
    if not source_ids:
        return []
    return ["", "Sources: " + " ".join(f"[{sid}]" for sid in source_ids)]


def _render_data_table(
    columns: Sequence[str], rows: Sequence[Sequence[object]], unverified_cells: Sequence[tuple[int, int]] = ()
) -> list[str]:
    flagged = {tuple(cell) for cell in unverified_cells}
    lines = [
        "| " + " | ".join(_escape_cell(c) for c in columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row_index, row in enumerate(rows):
        cells = []
        for col_index, cell in enumerate(row):
            text = _format_cell(cell)
            if (row_index, col_index) in flagged:
                text += " †"
            cells.append(_escape_cell(text))
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def _render_metric_value(metric: MetricItem) -> str:
    text = _format_number(metric.value)
    if metric.unit:
        text += metric.unit if metric.unit.strip() == "%" else f" {metric.unit}"
    if metric.context:
        text += f" ({metric.context})"
    return text


def _metrics_source_ids(metrics: Sequence[MetricItem]) -> list[str]:
    seen: list[str] = []
    for metric in metrics:
        for source_id in metric.source_ids:
            if source_id not in seen:
                seen.append(source_id)
    return seen


def _render_prose_block(block: ProseBlock) -> list[str]:
    return [block.markdown]


def _render_table_block(block: TableBlock) -> list[str]:
    lines: list[str] = []
    if block.title:
        lines.append(f"**{block.title}**")
        lines.append("")
    lines.extend(_render_data_table(block.columns, block.rows, block.unverified_cells))
    if block.unverified_cells:
        lines.append("")
        lines.append(block.caption or "† values were not found in retrieved evidence.")
    elif block.caption:
        lines.append("")
        lines.append(block.caption)
    lines.extend(_source_line(block.source_ids))
    return lines


def _render_metrics_block(block: MetricsBlock) -> list[str]:
    lines: list[str] = []
    if block.title:
        lines.append(f"**{block.title}**")
    for metric in block.metrics:
        lines.append(f"- **{metric.label}**: {_render_metric_value(metric)}")
    lines.extend(_source_line(_metrics_source_ids(block.metrics)))
    return lines


def _render_chart_block(block: ChartBlock, chart_mode: ChartMode) -> list[str]:
    if chart_mode == "omit":
        return []
    lines = [f"*Figure: {block.title}*"]
    header = ["Category", *[series.name for series in block.series]]
    rows = [
        [category, *[series.values[i] if i < len(series.values) else None for series in block.series]]
        for i, category in enumerate(block.categories)
    ]
    lines.append("")
    lines.extend(_render_data_table(header, rows))
    if block.verification == "estimate":
        lines.append("")
        lines.append("_Illustrative estimate — not from retrieved sources._")
    lines.extend(_source_line(block.source_ids))
    return lines


def _render_comparison_block(block: ComparisonBlock) -> list[str]:
    lines: list[str] = []
    if block.title:
        lines.append(f"**{block.title}**")
        lines.append("")
    header = ["Criterion", *block.options]
    rows = [[criterion.label, *criterion.values] for criterion in block.criteria]
    lines.extend(_render_data_table(header, rows))
    if block.recommendation:
        lines.append("")
        lines.append(f"_Recommendation: {block.recommendation}_")
    lines.extend(_source_line(block.source_ids))
    return lines


def _render_callout_block(block: CalloutBlock) -> list[str]:
    prefix = f"**{block.title}** — " if block.title else ""
    body_lines = block.markdown.splitlines() or [""]
    lines = [f"> {prefix}{body_lines[0]}"]
    lines.extend(f"> {line}" for line in body_lines[1:])
    return lines


def _render_timeline_block(block: TimelineBlock) -> list[str]:
    lines: list[str] = []
    if block.title:
        lines.append(f"**{block.title}**")
    for event in block.events:
        detail = f" — {event.detail}" if event.detail else ""
        lines.append(f"- **{event.date}**: {event.title}{detail}")
    lines.extend(_source_line(block.source_ids))
    return lines


def _render_artifact_block(block: ArtifactBlock) -> list[str]:
    lines = [f"> Interactive view: {block.title} — {block.purpose}"]
    if block.data is not None:
        lines.append("")
        lines.extend(_render_data_table(block.data.columns, block.data.rows, block.unverified_cells))
    lines.extend(_source_line(block.source_ids))
    return lines


def _render_block(block: Block, chart_mode: ChartMode) -> list[str]:
    if isinstance(block, ProseBlock):
        return _render_prose_block(block)
    if isinstance(block, TableBlock):
        return _render_table_block(block)
    if isinstance(block, MetricsBlock):
        return _render_metrics_block(block)
    if isinstance(block, ChartBlock):
        return _render_chart_block(block, chart_mode)
    if isinstance(block, ComparisonBlock):
        return _render_comparison_block(block)
    if isinstance(block, CalloutBlock):
        return _render_callout_block(block)
    if isinstance(block, TimelineBlock):
        return _render_timeline_block(block)
    if isinstance(block, ArtifactBlock):
        return _render_artifact_block(block)
    return []


def render_section_markdown(section: ResponseSection, *, chart_mode: ChartMode = "table") -> str:
    """Render one section's blocks (without its heading) to markdown."""
    lines: list[str] = []
    for block in section.blocks:
        lines.extend(_render_block(block, chart_mode))
        lines.append("")
    return "\n".join(lines).strip()


def render_document_markdown(
    doc: ResponseDocument,
    *,
    chart_mode: ChartMode = "table",
    include_title: bool = True,
) -> str:
    """Render a document to GFM markdown. Artifact HTML is never included."""
    lines: list[str] = []
    if include_title:
        lines.append(f"# {doc.title}")
        lines.append("")
    lines.append(doc.summary)
    lines.append("")
    if doc.key_metrics:
        for metric in doc.key_metrics:
            lines.append(f"- **{metric.label}**: {_render_metric_value(metric)}")
        lines.append("")

    for section in doc.sections:
        lines.append(f"## {section.heading}")
        lines.append("")
        lines.append(render_section_markdown(section, chart_mode=chart_mode))
        lines.append("")

    if doc.confidence:
        confidence_line = f"**Confidence: {doc.confidence}**"
        if doc.confidence_rationale:
            confidence_line += f" — {doc.confidence_rationale}"
        lines.append(confidence_line)
        lines.append("")

    if doc.open_questions:
        lines.append("**Open questions**")
        for question in doc.open_questions:
            lines.append(f"- {question}")
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def document_sections_for_export(doc: ResponseDocument) -> list[dict]:
    """Return export sections as ``{"heading", "content", "order"}`` dicts."""
    return [
        {
            "heading": section.heading,
            "content": render_section_markdown(section, chart_mode="omit"),
            "order": order,
        }
        for order, section in enumerate(doc.sections)
    ]


__all__ = [
    "ChartMode",
    "document_sections_for_export",
    "render_document_markdown",
    "render_section_markdown",
]
