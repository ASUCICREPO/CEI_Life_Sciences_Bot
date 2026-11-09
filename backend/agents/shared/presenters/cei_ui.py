"""Deterministic presenters for complete CEI UI surfaces."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence

from pydantic import ValidationError

from shared.presenters.markdown import render_section_markdown
from shared.schemas.response_blocks import (
    ArtifactBlock,
    Block,
    CalloutBlock,
    ChartBlock,
    ComparisonBlock,
    MetricItem,
    MetricsBlock,
    ProseBlock,
    ResponseDocument,
    ResponseSection,
    TableBlock,
    TimelineBlock,
)

from shared.schemas.cei_ui import (
    ARTIFACT_MAX_HEIGHT,
    ARTIFACT_MIN_HEIGHT,
    ArtifactComponent,
    CEIUISurface,
    CalloutComponent,
    CardComponent,
    CitationChipComponent,
    CitationDescriptor,
    ColumnComponent,
    ComparisonComponent,
    ComparisonCriterion,
    EvidencePanelComponent,
    GraphComponent,
    GraphSeries,
    ListComponent,
    MarkdownComponent,
    MAX_ARTIFACTS_PER_SURFACE,
    Metric,
    RowComponent,
    StatGridComponent,
    TabDescriptor,
    TableCellRef,
    TableColumn,
    TableComponent,
    TabsComponent,
    TextComponent,
    TimelineComponent,
    TimelineItem,
)

logger = logging.getLogger(__name__)

MAX_RENDERED_CITATIONS = 12
COMPONENT_BUDGET = 240
MAX_SECTION_CITATION_CHIPS = 5

_SECTION_SPLIT_RE = re.compile(r"^## +(.+)$", re.MULTILINE)
_REFERENCES_TAIL_RE = re.compile(r"\n-{2,}\n\*\*References\*\*\n.*\Z", re.DOTALL)
_REFERENCES_HEADING_RE = re.compile(r"\n#{1,2} ?References\b.*\Z", re.DOTALL)
_BRACKET_RE = re.compile(r"\[([^\]]+)\]")


# ── Shared block -> component helpers ────────────────────────────────────────


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)


def _format_display_number(value: float) -> str:
    # ADR: comma-grouped display value | StatGrid values are read at a glance, unlike markdown tables
    return format(value, ",g")


def _metric_to_stat(metric: MetricItem) -> Metric:
    return Metric(
        label=metric.label,
        value=_format_display_number(metric.value),
        numeric_value=metric.value,
        unit=metric.unit,
        context=metric.context,
        trend=metric.trend,
        sentiment="neutral",
        verified=metric.verification not in ("unverified", "estimate"),
    )


def _column_kind(rows: Sequence[Sequence[object]], column_index: int) -> str:
    values = [row[column_index] for row in rows if column_index < len(row) and row[column_index] is not None]
    if not values:
        return "text"
    numeric = sum(1 for value in values if isinstance(value, (int, float)) and not isinstance(value, bool))
    return "number" if numeric * 2 >= len(values) else "text"


def _data_table_component(
    component_id: str,
    columns: Sequence[str],
    rows: Sequence[Sequence[object]],
    unverified_cells: Sequence[tuple[int, int]] = (),
    *,
    title: str | None = None,
    caption: str | None = None,
) -> TableComponent:
    table_columns = [
        TableColumn(
            key=f"c{index}",
            label=header,
            align="right" if _column_kind(rows, index) == "number" else "left",
            kind=_column_kind(rows, index),
        )
        for index, header in enumerate(columns)
    ]
    flagged = [TableCellRef(row=row, column=column) for row, column in unverified_cells]
    if flagged and not caption:
        caption = "Values marked † were not found in retrieved evidence."
    return TableComponent(
        id=component_id,
        title=title,
        caption=caption,
        columns=table_columns,
        rows=[list(row) for row in rows],
        flagged_cells=flagged,
        sortable=len(rows) > 3,
    )


def _table_block_to_component(component_id: str, block: TableBlock) -> TableComponent:
    return _data_table_component(
        component_id,
        block.columns,
        block.rows,
        block.unverified_cells,
        title=block.title,
        caption=block.caption,
    )


def _chart_block_to_components(prefix: str, block: ChartBlock) -> tuple[list[object], list[str]]:
    graph_type = "bar-vertical" if block.chart_type == "bar" else block.chart_type
    graph = GraphComponent(
        id=f"{prefix}-chart",
        graph_type=graph_type,
        title=block.title,
        categories=list(block.categories),
        series=[GraphSeries(name=series.name, values=list(series.values)) for series in block.series],
        x_label=block.x_label,
        y_label=block.y_label,
    )
    if block.verification == "estimate":
        caption = TextComponent(
            id=f"{prefix}-chart-caption",
            content="Illustrative estimate — not from retrieved sources",
            variant="caption",
        )
        return [caption, graph], [caption.id, graph.id]
    return [graph], [graph.id]


def _comparison_block_to_component(component_id: str, block: ComparisonBlock) -> ComparisonComponent:
    return ComparisonComponent(
        id=component_id,
        title=block.title,
        options=list(block.options),
        criteria=[
            ComparisonCriterion(label=criterion.label, values=list(criterion.values))
            for criterion in block.criteria
        ],
        recommendation=block.recommendation,
    )


_CALLOUT_TONE_MAP = {
    "key_finding": "success",
    "warning": "warning",
    "limitation": "neutral",
    "assumption": "neutral",
    "info": "info",
}


def _callout_block_to_component(component_id: str, block: CalloutBlock) -> CalloutComponent:
    return CalloutComponent(
        id=component_id,
        tone=_CALLOUT_TONE_MAP.get(block.tone, "info"),
        title=block.title,
        content=block.markdown,
    )


def _timeline_block_to_component(component_id: str, block: TimelineBlock) -> TimelineComponent:
    return TimelineComponent(
        id=component_id,
        title=block.title,
        items=[
            TimelineItem(
                item_id=f"e{index}",
                date_label=event.date,
                title=event.title,
                description=event.detail,
                status=event.status,
            )
            for index, event in enumerate(block.events)
        ],
    )


def _artifact_block_to_components(
    prefix: str, block: ArtifactBlock, artifact_slot_available: bool
) -> tuple[list[object], list[str], bool]:
    data_payload = None
    if block.data is not None:
        data_payload = {"columns": list(block.data.columns), "rows": [list(row) for row in block.data.rows]}

    if artifact_slot_available:
        try:
            artifact = ArtifactComponent(
                id=f"{prefix}-artifact",
                title=block.title,
                description=block.purpose,
                html=block.html,
                data=data_payload,
                height=min(max(block.height, ARTIFACT_MIN_HEIGHT), ARTIFACT_MAX_HEIGHT),
            )
            components: list[object] = [artifact]
            ids = [artifact.id]
            if block.data is not None:
                table = _data_table_component(
                    f"{prefix}-artifact-data", block.data.columns, block.data.rows, block.unverified_cells
                )
                components.append(table)
                ids.append(table.id)
            return components, ids, True
        except ValidationError:
            logger.warning({"event": "artifact_degraded"})

    fallback = CalloutComponent(
        id=f"{prefix}-artifact",
        tone="warning",
        title="Interactive view unavailable",
        content="This interactive view could not be shown safely; the underlying data is shown below.",
    )
    components = [fallback]
    ids = [fallback.id]
    if block.data is not None:
        table = _data_table_component(
            f"{prefix}-artifact-data", block.data.columns, block.data.rows, block.unverified_cells
        )
        components.append(table)
        ids.append(table.id)
    return components, ids, False


def _block_to_components(
    prefix: str, block: Block, artifact_slot_available: bool
) -> tuple[list[object], list[str], bool]:
    """Map one model block to one or more CEI UI components. Returns (components, child ids, used_artifact_slot)."""
    if isinstance(block, ProseBlock):
        component = MarkdownComponent(id=f"{prefix}-prose", content=block.markdown)
        return [component], [component.id], False
    if isinstance(block, TableBlock):
        component = _table_block_to_component(f"{prefix}-table", block)
        return [component], [component.id], False
    if isinstance(block, MetricsBlock):
        component = StatGridComponent(
            id=f"{prefix}-metrics",
            title=block.title,
            metrics=[_metric_to_stat(metric) for metric in block.metrics],
        )
        return [component], [component.id], False
    if isinstance(block, ChartBlock):
        components, ids = _chart_block_to_components(prefix, block)
        return components, ids, False
    if isinstance(block, ComparisonBlock):
        component = _comparison_block_to_component(f"{prefix}-comparison", block)
        return [component], [component.id], False
    if isinstance(block, CalloutBlock):
        component = _callout_block_to_component(f"{prefix}-callout", block)
        return [component], [component.id], False
    if isinstance(block, TimelineBlock):
        component = _timeline_block_to_component(f"{prefix}-timeline", block)
        return [component], [component.id], False
    if isinstance(block, ArtifactBlock):
        return _artifact_block_to_components(f"{prefix}-artifact", block, artifact_slot_available)
    return [], [], False


def _block_source_ids(block: Block) -> list[str]:
    if isinstance(block, MetricsBlock):
        seen: list[str] = []
        for metric in block.metrics:
            for source_id in metric.source_ids:
                if source_id not in seen:
                    seen.append(source_id)
        return seen
    return list(getattr(block, "source_ids", []) or [])


def _block_inline_text(block: Block) -> str | None:
    if isinstance(block, ProseBlock):
        return block.markdown
    if isinstance(block, CalloutBlock):
        return block.markdown
    return None


def _section_citation_ids(section: ResponseSection, known_citation_ids: set[str]) -> list[str]:
    ids: list[str] = []

    def add(source_id: str) -> None:
        if source_id in known_citation_ids and source_id not in ids:
            ids.append(source_id)

    for block in section.blocks:
        for source_id in _block_source_ids(block):
            add(source_id)
        text = _block_inline_text(block)
        if text:
            for group in _BRACKET_RE.findall(text):
                for source_id in re.split(r"[,;]", group):
                    add(source_id.strip())
    return ids[:MAX_SECTION_CITATION_CHIPS]


def _norm_heading(text: str) -> str:
    return re.sub(r"[*_`#]", "", text).strip().lower()


# ── Markdown splitting shared by research and drafting legacy paths ─────────


def strip_references_tail(text: str) -> str:
    stripped = _REFERENCES_TAIL_RE.sub("", text)
    if stripped != text:
        return stripped.rstrip()
    match = _REFERENCES_HEADING_RE.search(text)
    if match:
        return text[: match.start()].rstrip()
    return text


def split_markdown_sections(body: str) -> list[tuple[str, str]]:
    matches = list(_SECTION_SPLIT_RE.finditer(body))
    if not matches:
        return [("Answer", body.strip())] if body.strip() else []
    sections: list[tuple[str, str]] = []
    lead = body[: matches[0].start()].strip()
    if lead:
        sections.append(("Overview", lead))
    for index, match in enumerate(matches):
        heading = match.group(1).strip()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        sections.append((heading, body[start:end].strip()))
    return sections


# ── Research surface ─────────────────────────────────────────────────────────


def build_final_text_surface(text: str, *, surface_id: str) -> CEIUISurface:
    """Build a complete surface for a plain final response."""
    return CEIUISurface(
        surface_id=surface_id,
        root_id="root",
        components=[
            ColumnComponent(id="root", children=["response-text"]),
            MarkdownComponent(id="response-text", content=text),
        ],
    )


def _collect_citations(
    retrieved_results: Sequence[object],
) -> tuple[list[CitationDescriptor], int]:
    citations: list[CitationDescriptor] = []
    seen_sources: set[tuple[str, str]] = set()
    total_citations = 0

    for result in retrieved_results:
        source_type_value = getattr(result, "source_type", "unknown")
        source_type = str(getattr(source_type_value, "value", source_type_value))
        for raw_citation in getattr(result, "citations", []):
            source_id = str(getattr(raw_citation, "source_id", ""))
            url = str(getattr(raw_citation, "url", ""))
            source_key = (source_id, url)
            if not source_id or source_key in seen_sources or not url.startswith("https://"):
                continue
            seen_sources.add(source_key)
            total_citations += 1
            if len(citations) >= MAX_RENDERED_CITATIONS:
                continue
            raw_year = getattr(raw_citation, "year", None)
            citations.append(
                CitationDescriptor(
                    citation_id=source_id,
                    title=str(getattr(raw_citation, "title", source_id)),
                    source_type=source_type,
                    url=url,
                    year=raw_year if isinstance(raw_year, int) else None,
                    relevant_passage=_optional_text(getattr(raw_citation, "relevant_passage", None)),
                )
            )
    return citations, total_citations


def _build_evidence_components(citations: list[CitationDescriptor], total_citations: int) -> list[object]:
    citation_ids = [f"citation-{index}" for index in range(len(citations))]
    return [
        EvidencePanelComponent(
            id="evidence-panel",
            citations=citations,
            summary=f"Showing {len(citations)} of {total_citations} citations",
            children=citation_ids,
        ),
        *[
            CitationChipComponent(id=component_id, citation=citation)
            for component_id, citation in zip(citation_ids, citations, strict=True)
        ],
    ]


def build_research_surface(
    text: str,
    *,
    surface_id: str,
    retrieved_results: Sequence[object],
    document: ResponseDocument | None = None,
) -> CEIUISurface:
    """Build a research surface with deduplicated, bounded evidence."""
    citations, total_citations = _collect_citations(retrieved_results)
    evidence_components = _build_evidence_components(citations, total_citations)

    if document is not None:
        try:
            return _build_research_document_surface(surface_id, document, citations, evidence_components)
        except ValidationError:
            logger.warning({"event": "research_document_surface_fallback"})

    return _build_research_legacy_surface(text, surface_id, evidence_components)


def _build_confidence_callout(document: ResponseDocument) -> CalloutComponent:
    title = f"Confidence: {document.confidence}" if document.confidence else "Confidence"
    content = document.confidence_rationale or "No explicit confidence rationale was provided."
    return CalloutComponent(id="confidence-callout", tone="info", title=title, content=content)


def _build_open_questions_callout(open_questions: Sequence[str]) -> CalloutComponent:
    return CalloutComponent(
        id="open-questions",
        tone="neutral",
        title="Open questions",
        content="\n".join(f"- {question}" for question in open_questions),
    )


def _build_rich_section(
    section_index: int,
    section: ResponseSection,
    known_citation_ids: set[str],
    citation_by_id: Mapping[str, CitationDescriptor],
    artifacts_used: int,
) -> tuple[list[object], str, int]:
    components: list[object] = []
    child_ids: list[str] = []
    for block_index, block in enumerate(section.blocks):
        prefix = f"s{section_index}-b{block_index}"
        block_components, block_ids, used_slot = _block_to_components(
            prefix, block, artifacts_used < MAX_ARTIFACTS_PER_SURFACE
        )
        if used_slot:
            artifacts_used += 1
        components.extend(block_components)
        child_ids.extend(block_ids)

    chip_ids = _section_citation_ids(section, known_citation_ids)
    if chip_ids:
        row_id = f"s{section_index}-sources"
        chip_component_ids = [f"s{section_index}-chip-{index}" for index in range(len(chip_ids))]
        components.extend(
            CitationChipComponent(id=cid, citation=citation_by_id[source_id])
            for cid, source_id in zip(chip_component_ids, chip_ids, strict=True)
        )
        components.append(RowComponent(id=row_id, children=chip_component_ids))
        child_ids.append(row_id)

    card_id = f"s{section_index}-card"
    components.append(CardComponent(id=card_id, title=section.heading, children=child_ids))
    return components, card_id, artifacts_used


def _build_collapsed_section(section_index: int, section: ResponseSection) -> tuple[list[object], str]:
    markdown_id = f"s{section_index}-b0-markdown"
    card_id = f"s{section_index}-card"
    markdown_text = render_section_markdown(section, chart_mode="table")
    return (
        [
            MarkdownComponent(id=markdown_id, content=markdown_text),
            CardComponent(id=card_id, title=section.heading, children=[markdown_id]),
        ],
        card_id,
    )


def _build_document_sections(
    document: ResponseDocument,
    known_citation_ids: set[str],
    citation_by_id: Mapping[str, CitationDescriptor],
    reserved: int,
) -> tuple[list[object], list[str]]:
    """Build section cards, collapsing later sections into markdown once the budget is reached."""
    components: list[object] = []
    section_card_ids: list[str] = []
    used = reserved
    artifacts_used = 0
    collapsed = False

    for section_index, section in enumerate(document.sections):
        if not collapsed:
            section_components, card_id, next_artifacts_used = _build_rich_section(
                section_index, section, known_citation_ids, citation_by_id, artifacts_used
            )
            if used + len(section_components) <= COMPONENT_BUDGET:
                components.extend(section_components)
                section_card_ids.append(card_id)
                used += len(section_components)
                artifacts_used = next_artifacts_used
                continue
            collapsed = True
        collapsed_components, card_id = _build_collapsed_section(section_index, section)
        components.extend(collapsed_components)
        section_card_ids.append(card_id)
        used += len(collapsed_components)

    return components, section_card_ids


def _build_research_document_surface(
    surface_id: str,
    document: ResponseDocument,
    citations: list[CitationDescriptor],
    evidence_components: list[object],
) -> CEIUISurface:
    citation_by_id = {citation.citation_id: citation for citation in citations}
    known_citation_ids = set(citation_by_id)

    header_children = ["header-summary"]
    components: list[object] = [MarkdownComponent(id="header-summary", content=document.summary)]
    if document.key_metrics:
        header_children.append("header-metrics")
        components.append(
            StatGridComponent(
                id="header-metrics", metrics=[_metric_to_stat(metric) for metric in document.key_metrics]
            )
        )
    components.append(CardComponent(id="header-card", title=document.title, children=header_children))

    reserved = len(components) + len(evidence_components) + 2  # confidence callout + open questions
    section_components, section_card_ids = _build_document_sections(
        document, known_citation_ids, citation_by_id, reserved
    )
    components.extend(section_components)

    components.append(_build_confidence_callout(document))
    answer_children = ["header-card", *section_card_ids, "confidence-callout"]
    if document.open_questions:
        components.append(_build_open_questions_callout(document.open_questions))
        answer_children.append("open-questions")

    components.append(ColumnComponent(id="answer-col", children=answer_children))
    components.append(
        TabsComponent(
            id="root",
            tabs=[
                TabDescriptor(label="Answer", child_id="answer-col"),
                TabDescriptor(label="Evidence", child_id="evidence-panel"),
            ],
        )
    )
    components.extend(evidence_components)
    return CEIUISurface(surface_id=surface_id, root_id="root", components=components)


def _build_research_legacy_surface(text: str, surface_id: str, evidence_components: list[object]) -> CEIUISurface:
    body = strip_references_tail(text)
    sections = split_markdown_sections(body)

    if len(sections) <= 1:
        components = [
            TabsComponent(
                id="root",
                tabs=[
                    TabDescriptor(label="Answer", child_id="answer-card"),
                    TabDescriptor(label="Evidence", child_id="evidence-panel"),
                ],
            ),
            CardComponent(id="answer-card", title="Answer", children=["answer-text"]),
            MarkdownComponent(id="answer-text", content=body),
            *evidence_components,
        ]
        return CEIUISurface(surface_id=surface_id, root_id="root", components=components)

    section_card_ids: list[str] = []
    components: list[object] = []
    for index, (heading, content) in enumerate(sections):
        markdown_id = f"legacy-s{index}-markdown"
        card_id = f"legacy-s{index}-card"
        components.append(MarkdownComponent(id=markdown_id, content=content))
        components.append(CardComponent(id=card_id, title=heading, children=[markdown_id]))
        section_card_ids.append(card_id)

    components.append(ColumnComponent(id="answer-col", children=section_card_ids))
    components.append(
        TabsComponent(
            id="root",
            tabs=[
                TabDescriptor(label="Answer", child_id="answer-col"),
                TabDescriptor(label="Evidence", child_id="evidence-panel"),
            ],
        )
    )
    components.extend(evidence_components)
    return CEIUISurface(surface_id=surface_id, root_id="root", components=components)


# ── Drafting surface ─────────────────────────────────────────────────────────


def _chart_spec_to_graph(component_id: str, spec: Mapping[str, object]) -> GraphComponent | None:
    data = spec.get("data")
    if not isinstance(data, Mapping):
        return None
    categories = data.get("x")
    if not isinstance(categories, list) or not categories:
        return None
    try:
        series_items = [
            GraphSeries(name=str(name), values=[float(v) for v in values])
            for name, values in data.items()
            if name != "x" and isinstance(values, list) and values
        ]
        if not series_items:
            return None
        chart_type = str(spec.get("chart_type", "bar"))
        graph_type = "bar-vertical" if chart_type == "bar" else chart_type
        return GraphComponent(
            id=component_id,
            graph_type=graph_type,
            title=str(spec.get("title", "Chart")),
            categories=[str(c) for c in categories],
            series=series_items,
            x_label=_optional_text(spec.get("x_label")) or None,
            y_label=_optional_text(spec.get("y_label")) or None,
        )
    except (ValidationError, TypeError, ValueError):
        return None


def _format_fields(value: Mapping[str, object], fields: Sequence[str]) -> str:
    return "\n".join(
        f"{field.replace('_', ' ').title()}: {value[field]}" for field in fields if field in value
    )


def build_drafting_surface(
    text: str,
    *,
    surface_id: str,
    exports: Sequence[Mapping[str, object]],
    chart_specs: Sequence[Mapping[str, object]],
    chart_instructions: str | None,
    document: ResponseDocument | None = None,
) -> CEIUISurface:
    """Build a drafting surface without private export or chart fields."""
    deliverables_components = _build_deliverables_components(exports, chart_specs, chart_instructions)

    if document is not None:
        try:
            return _build_drafting_document_surface(
                surface_id, document, chart_specs, chart_instructions, deliverables_components
            )
        except ValidationError:
            logger.warning({"event": "drafting_document_surface_fallback"})

    return _build_drafting_legacy_surface(text, surface_id, chart_specs, chart_instructions, deliverables_components)


def _build_deliverables_components(
    exports: Sequence[Mapping[str, object]],
    chart_specs: Sequence[Mapping[str, object]],
    chart_instructions: str | None,
) -> list[object]:
    safe_exports = [
        {key: export_item[key] for key in ("format", "filename", "byte_size") if key in export_item}
        for export_item in exports
    ]
    safe_charts = [
        {
            key: chart[key]
            for key in ("title", "chart_type", "x_label", "y_label", "section")
            if key in chart
        }
        for chart in chart_specs
    ]
    export_ids = [f"export-{index}" for index in range(len(safe_exports))]
    chart_ids = [f"chart-{index}" for index in range(len(safe_charts))]
    return [
        TextComponent(
            id="status-text",
            content=chart_instructions or "All requested chart data is available.",
        ),
        TextComponent(id="exports-heading", content=f"Exports ({len(safe_exports)})", variant="h2"),
        ListComponent(id="export-list", children=export_ids),
        TextComponent(id="charts-heading", content=f"Charts ({len(safe_charts)})", variant="h2"),
        ListComponent(id="chart-list", children=chart_ids),
        *[
            CardComponent(
                id=component_id,
                title=str(export_item.get("filename", "Export")),
                body=_format_fields(export_item, ("format", "byte_size")),
            )
            for component_id, export_item in zip(export_ids, safe_exports, strict=True)
        ],
        *[
            CardComponent(
                id=component_id,
                title=str(chart.get("title", "Chart")),
                body="\n".join(
                    filter(
                        None,
                        [_format_fields(chart, ("chart_type", "section", "x_label", "y_label")), "Shown in preview"],
                    )
                ),
            )
            for component_id, chart in zip(chart_ids, safe_charts, strict=True)
        ],
        ColumnComponent(
            id="deliverables-column",
            children=[
                "status-text",
                "exports-heading",
                "export-list",
                "charts-heading",
                "chart-list",
            ],
        ),
    ]


def _build_drafting_document_surface(
    surface_id: str,
    document: ResponseDocument,
    chart_specs: Sequence[Mapping[str, object]],
    chart_instructions: str | None,
    deliverables_components: list[object],
) -> CEIUISurface:
    doc_chart_titles = {
        block.title.strip().lower()
        for section in document.sections
        for block in section.blocks
        if isinstance(block, ChartBlock)
    }

    header_children = ["header-summary"]
    components: list[object] = [MarkdownComponent(id="header-summary", content=document.summary)]
    if document.key_metrics:
        header_children.append("header-metrics")
        components.append(
            StatGridComponent(
                id="header-metrics", metrics=[_metric_to_stat(metric) for metric in document.key_metrics]
            )
        )
    components.append(CardComponent(id="header-card", title=document.title, children=header_children))

    reserved = len(components) + len(deliverables_components)
    section_components, section_card_ids = _build_document_sections(document, set(), {}, reserved)
    components.extend(section_components)
    section_by_heading = {
        _norm_heading(section.heading): card_id
        for section, card_id in zip(document.sections, section_card_ids, strict=True)
    }

    figures_children: list[str] = []
    for index, spec in enumerate(chart_specs):
        title = str(spec.get("title", "")).strip().lower()
        if title in doc_chart_titles:
            continue
        graph = _chart_spec_to_graph(f"draft-chart-{index}", spec)
        if graph is None:
            continue
        components.append(graph)
        target_card_id = section_by_heading.get(_norm_heading(str(spec.get("section", ""))))
        if target_card_id is not None:
            _append_child(components, target_card_id, graph.id)
        else:
            figures_children.append(graph.id)

    if figures_children:
        components.append(CardComponent(id="figures-card", title="Figures", children=figures_children))
        section_card_ids.append("figures-card")

    answer_children = ["header-card", *section_card_ids]
    if chart_instructions:
        components.append(_build_chart_instructions_callout(chart_instructions))
        answer_children.append("chart-instructions-callout")

    components.append(ColumnComponent(id="document-col", children=answer_children))
    components.append(
        TabsComponent(
            id="root",
            tabs=[
                TabDescriptor(label="Preview", child_id="document-col"),
                TabDescriptor(label="Deliverables", child_id="deliverables-column"),
            ],
        )
    )
    components.extend(deliverables_components)
    return CEIUISurface(surface_id=surface_id, root_id="root", components=components)


def _build_chart_instructions_callout(chart_instructions: str) -> CalloutComponent:
    return CalloutComponent(
        id="chart-instructions-callout",
        tone="warning",
        title="Chart data needed",
        content=chart_instructions,
    )


def _append_child(components: list[object], card_id: str, child_id: str) -> None:
    for index, component in enumerate(components):
        if getattr(component, "id", None) == card_id:
            components[index] = component.model_copy(update={"children": [*component.children, child_id]})
            return


def _build_drafting_legacy_surface(
    text: str,
    surface_id: str,
    chart_specs: Sequence[Mapping[str, object]],
    chart_instructions: str | None,
    deliverables_components: list[object],
) -> CEIUISurface:
    sections = split_markdown_sections(text)
    graphs = [
        (str(spec.get("section", "")), graph)
        for index, spec in enumerate(chart_specs)
        if (graph := _chart_spec_to_graph(f"draft-chart-{index}", spec)) is not None
    ]

    if len(sections) <= 1:
        figures_children = [graph.id for _, graph in graphs]
        components: list[object] = [
            CardComponent(id="document-card", title="Document", children=["document-text"]),
            MarkdownComponent(id="document-text", content=text),
            *[graph for _, graph in graphs],
        ]
        answer_children = ["document-card"]
        if figures_children:
            components.append(CardComponent(id="figures-card", title="Figures", children=figures_children))
            answer_children.append("figures-card")
        if chart_instructions:
            components.append(_build_chart_instructions_callout(chart_instructions))
            answer_children.append("chart-instructions-callout")
        components.append(ColumnComponent(id="document-col", children=answer_children))
        components.append(
            TabsComponent(
                id="root",
                tabs=[
                    TabDescriptor(label="Preview", child_id="document-col"),
                    TabDescriptor(label="Deliverables", child_id="deliverables-column"),
                ],
            )
        )
        components.extend(deliverables_components)
        return CEIUISurface(surface_id=surface_id, root_id="root", components=components)

    section_card_ids: list[str] = []
    section_by_heading: dict[str, str] = {}
    components = []
    for index, (heading, content) in enumerate(sections):
        markdown_id = f"legacy-s{index}-markdown"
        card_id = f"legacy-s{index}-card"
        components.append(MarkdownComponent(id=markdown_id, content=content))
        components.append(CardComponent(id=card_id, title=heading, children=[markdown_id]))
        section_card_ids.append(card_id)
        section_by_heading[_norm_heading(heading)] = card_id

    figures_children = []
    for wanted_heading, graph in graphs:
        components.append(graph)
        target_card_id = section_by_heading.get(_norm_heading(wanted_heading))
        if target_card_id is not None:
            _append_child(components, target_card_id, graph.id)
        else:
            figures_children.append(graph.id)

    if figures_children:
        components.append(CardComponent(id="figures-card", title="Figures", children=figures_children))
        section_card_ids.append("figures-card")

    if chart_instructions:
        components.append(_build_chart_instructions_callout(chart_instructions))
        section_card_ids.append("chart-instructions-callout")

    components.append(ColumnComponent(id="document-col", children=section_card_ids))
    components.append(
        TabsComponent(
            id="root",
            tabs=[
                TabDescriptor(label="Preview", child_id="document-col"),
                TabDescriptor(label="Deliverables", child_id="deliverables-column"),
            ],
        )
    )
    components.extend(deliverables_components)
    return CEIUISurface(surface_id=surface_id, root_id="root", components=components)
