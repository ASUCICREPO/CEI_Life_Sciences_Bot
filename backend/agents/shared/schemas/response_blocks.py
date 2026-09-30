"""Model-facing structured response blocks.

Synthesis and drafting models return a ``ResponseDocument`` through a tool
call. Deterministic presenters map the blocks to CEI UI components; models
never emit component records. These models are lax on purpose: they coerce
common model output quirks and truncate oversize lists instead of failing.
Fields wrapped in ``SkipJsonSchema`` are set by the server after grounding and
are hidden from the tool schema.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, Field
from pydantic.json_schema import SkipJsonSchema

Basis = Literal["sourced", "estimate"]
Verification = Literal["verified", "unverified", "estimate"]

# Render limit of one StatGrid component; not shown to the model.
MAX_STATGRID_METRICS = 12
MAX_BLOCK_TABLE_COLUMNS = 12
MAX_BLOCK_TABLE_ROWS = 100
MAX_CHART_CATEGORIES = 100
MAX_CHART_SERIES = 8
MAX_COMPARISON_OPTIONS = 6
MAX_COMPARISON_CRITERIA = 20
MAX_TIMELINE_EVENTS = 30
MAX_ARTIFACT_HTML_CHARS = 60_000


def _decode_json_string(value: Any) -> Any:
    """Models sometimes return nested objects as JSON-encoded strings."""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped[:1] in ("[", "{"):
            try:
                return json.loads(stripped)
            except ValueError:
                return value
    return value


def _capped(limit: int) -> BeforeValidator:
    def truncate(value: Any) -> Any:
        value = _decode_json_string(value)
        return value[:limit] if isinstance(value, list) else value

    return BeforeValidator(truncate)


def _dedupe_strings(value: Any) -> Any:
    value = _decode_json_string(value)
    if not isinstance(value, list):
        return value
    return list(dict.fromkeys(str(item) for item in value if str(item).strip()))


def _stringify_list(value: Any) -> Any:
    value = _decode_json_string(value)
    if not isinstance(value, list):
        return value
    return [str(item) for item in value]


SourceIds = Annotated[
    list[str],
    BeforeValidator(_dedupe_strings),
    Field(
        default_factory=list,
        description="Evidence source IDs (for example PMID:123) that support this block.",
    ),
]


class ProseBlock(BaseModel):
    kind: Literal["prose"]
    markdown: str = Field(
        description="Markdown explanation or interpretation with inline [SourceID] citations."
    )


class DataTable(BaseModel):
    columns: Annotated[
        list[str],
        _capped(MAX_BLOCK_TABLE_COLUMNS),
        BeforeValidator(_stringify_list),
        Field(min_length=1, max_length=MAX_BLOCK_TABLE_COLUMNS, description="Column headers."),
    ]
    rows: Annotated[
        list[list[str | float | None]],
        _capped(MAX_BLOCK_TABLE_ROWS),
        Field(
            default_factory=list,
            max_length=MAX_BLOCK_TABLE_ROWS,
            description="Rows with one cell per column. Use numbers for numeric cells.",
        ),
    ]


class TableBlock(DataTable):
    kind: Literal["table"]
    title: str | None = None
    caption: str | None = None
    basis: Basis = "sourced"
    source_ids: SourceIds
    verification: SkipJsonSchema[Verification | None] = None
    unverified_cells: SkipJsonSchema[list[tuple[int, int]]] = Field(default_factory=list)


class MetricItem(BaseModel):
    label: str = Field(description="Short metric name, for example 'Overall response rate'.")
    value: float = Field(description="Numeric value copied exactly from the evidence.")
    unit: str | None = Field(default=None, description="Unit such as '%', 'mg', 'USD bn'.")
    context: str | None = Field(
        default=None, description="Short qualifier, for example 'Phase 3, n=1,204'."
    )
    trend: Literal["up", "down", "flat"] | None = None
    basis: Basis = "sourced"
    source_ids: SourceIds
    verification: SkipJsonSchema[Verification | None] = None


class MetricsBlock(BaseModel):
    kind: Literal["metrics"]
    title: str | None = None
    metrics: Annotated[
        list[MetricItem],
        _capped(MAX_STATGRID_METRICS),
        Field(min_length=1),
    ]


class ChartSeriesItem(BaseModel):
    name: str
    values: Annotated[
        list[float],
        _capped(MAX_CHART_CATEGORIES),
        Field(max_length=MAX_CHART_CATEGORIES, description="One value per category."),
    ]


class ChartBlock(BaseModel):
    kind: Literal["chart"]
    title: str
    chart_type: Literal["bar", "bar-horizontal", "line", "scatter", "pie"] = "bar"
    categories: Annotated[
        list[str],
        _capped(MAX_CHART_CATEGORIES),
        BeforeValidator(_stringify_list),
        Field(min_length=1, max_length=MAX_CHART_CATEGORIES, description="X-axis categories."),
    ]
    series: Annotated[
        list[ChartSeriesItem],
        _capped(MAX_CHART_SERIES),
        Field(min_length=1, max_length=MAX_CHART_SERIES),
    ]
    x_label: str | None = None
    y_label: str | None = None
    basis: Basis = "sourced"
    source_ids: SourceIds
    verification: SkipJsonSchema[Verification | None] = None


class ComparisonCriterionItem(BaseModel):
    label: str
    values: Annotated[
        list[str],
        BeforeValidator(_stringify_list),
        Field(description="One value per option, in option order."),
    ]


class ComparisonBlock(BaseModel):
    kind: Literal["comparison"]
    title: str | None = None
    options: Annotated[
        list[str],
        _capped(MAX_COMPARISON_OPTIONS),
        BeforeValidator(_stringify_list),
        Field(min_length=2, max_length=MAX_COMPARISON_OPTIONS, description="Entities compared."),
    ]
    criteria: Annotated[
        list[ComparisonCriterionItem],
        _capped(MAX_COMPARISON_CRITERIA),
        Field(min_length=1, max_length=MAX_COMPARISON_CRITERIA),
    ]
    recommendation: str | None = None
    source_ids: SourceIds


class CalloutBlock(BaseModel):
    kind: Literal["callout"]
    tone: Literal["key_finding", "warning", "limitation", "assumption", "info"] = "info"
    title: str | None = None
    markdown: str = Field(description="One to three sentences with inline [SourceID] citations.")


class TimelineEvent(BaseModel):
    date: str = Field(description="Date label, for example '2023-Q2' or 'Mar 2021'.")
    title: str
    detail: str | None = None
    status: Literal["completed", "in_progress", "planned", "blocked"] | None = None


class TimelineBlock(BaseModel):
    kind: Literal["timeline"]
    title: str | None = None
    events: Annotated[
        list[TimelineEvent],
        _capped(MAX_TIMELINE_EVENTS),
        Field(min_length=1, max_length=MAX_TIMELINE_EVENTS),
    ]
    source_ids: SourceIds


class ArtifactBlock(BaseModel):
    kind: Literal["artifact"]
    title: str
    purpose: str = Field(description="One sentence that says what the interactive view shows.")
    html: str = Field(
        description=(
            "Self-contained HTML body fragment with inline <style> and <script>. "
            "Vanilla JS, SVG, or canvas only. Read numbers from "
            "JSON.parse(document.getElementById('cei-data').textContent). "
            "No URLs, network calls, storage, forms, links, or navigation."
        )
    )
    height: int = Field(default=420, description="Frame height in pixels, 120 to 1200.")
    data: DataTable | None = Field(
        default=None, description="Numbers the artifact visualizes, as a table."
    )
    basis: Basis = "sourced"
    source_ids: SourceIds
    verification: SkipJsonSchema[Verification | None] = None
    unverified_cells: SkipJsonSchema[list[tuple[int, int]]] = Field(default_factory=list)


Block = (
    ProseBlock
    | TableBlock
    | MetricsBlock
    | ChartBlock
    | ComparisonBlock
    | CalloutBlock
    | TimelineBlock
    | ArtifactBlock
)


class ResponseSection(BaseModel):
    heading: str
    blocks: Annotated[
        list[Block],
        BeforeValidator(_decode_json_string),
        Field(min_length=1),
    ]


class ResponseDocument(BaseModel):
    """A structured answer or document made of typed presentation blocks."""

    title: str
    summary: str = Field(description="A direct answer to the request.")
    key_metrics: Annotated[
        list[MetricItem],
        _capped(MAX_STATGRID_METRICS),
        Field(default_factory=list, description="Headline figures shown at the top."),
    ]
    sections: Annotated[
        list[ResponseSection],
        BeforeValidator(_decode_json_string),
        Field(min_length=1),
    ]
    confidence: Literal["high", "medium", "low"] | None = None
    confidence_rationale: str | None = None
    open_questions: Annotated[
        list[str],
        BeforeValidator(_stringify_list),
        Field(default_factory=list),
    ]


__all__ = [
    "ArtifactBlock",
    "Basis",
    "Block",
    "CalloutBlock",
    "ChartBlock",
    "ChartSeriesItem",
    "ComparisonBlock",
    "ComparisonCriterionItem",
    "DataTable",
    "MetricItem",
    "MetricsBlock",
    "ProseBlock",
    "ResponseDocument",
    "ResponseSection",
    "TableBlock",
    "TimelineBlock",
    "TimelineEvent",
    "Verification",
]
