"""Canonical CEI UI protocol models and validation."""

from __future__ import annotations

import json
import re
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, field_validator, model_validator


def _validate_https_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("URLs must use HTTPS and include a host")
    return value

CEI_UI_CONTENT_TYPE = "application/vnd.cei.ui+json"
CEI_UI_PROTOCOL = "cei-ui"
CEI_UI_VERSION = "1.1"
CEI_UI_SUPPORTED_VERSIONS = ("1.0", "1.1")
MAX_COMPONENTS = 250
MAX_COMPONENT_DEPTH = 32
MAX_CHILDREN = 250
MAX_ACTIONS = 20
MAX_TABS = 20
MAX_OPTIONS = 100
MAX_CITATIONS = 100
MAX_PLAN_STEPS = 100
MAX_GRAPH_SERIES = 20
MAX_GRAPH_CATEGORIES = 1_000
MAX_GRAPH_VALUES = 1_000
MAX_GRAPH_TOTAL_POINTS = 5_000
MAX_ACTION_VALUE_DEPTH = 8
MAX_ACTION_VALUE_NODES = 1_000
MAX_ACTION_VALUE_MAPPING_ENTRIES = 100
MAX_ACTION_VALUE_ARRAY_ENTRIES = 100
MAX_ACTION_VALUE_KEY_LENGTH = 128
MAX_ACTION_VALUE_STRING_LENGTH = 10_000
MAX_TABLE_COLUMNS = 12
MAX_TABLE_ROWS = 200
MAX_TABLE_CELLS = 2_000
MAX_METRICS = 12
MAX_TIMELINE_ITEMS = 50
MAX_COMPARISON_OPTIONS = 6
MAX_COMPARISON_CRITERIA = 30
MAX_ARTIFACT_HTML_CHARS = 65_536
MAX_ARTIFACT_DATA_BYTES = 65_536
MAX_ARTIFACTS_PER_SURFACE = 4
MAX_ARTIFACT_TOTAL_BYTES = 262_144
ARTIFACT_MIN_HEIGHT = 120
ARTIFACT_MAX_HEIGHT = 1_200
ARTIFACT_DEFAULT_HEIGHT = 420

# Defense-in-depth lint for model-authored artifact HTML. The sandboxed iframe
# and its CSP are the security boundary; these patterns reject obvious attempts
# at network access, navigation, storage, and frame escape. The frontend
# mirrors this list in lib/cei-ui/schema.ts. Patterns are case-insensitive.
ARTIFACT_HTML_DENYLIST: tuple[str, ...] = (
    r"<\s*/?\s*(html|head|meta|base|link|iframe|frame|frameset|object|embed|portal|form|a)(\s|/|>)",
    r"https?:",
    r"//[a-z0-9-]+\.[a-z]",
    r"javascript:",
    r"srcdoc",
    r"<\s*script[^>]*\bsrc\s*=",
    r"\b(fetch|importScripts|import)\s*\(",
    r"\b(XMLHttpRequest|WebSocket|EventSource|sendBeacon|RTCPeerConnection)\b",
    r"\bnew\s+(Shared)?Worker\b",
    r"\b(window|self|top|parent|opener|frames|globalThis)\s*\.\s*(location|postMessage|open|opener|parent|top|document)\b",
    r"\blocation\s*\.\s*(href|assign|replace|reload)\b",
    r"\blocation\s*=",
    r"\bpostMessage\b",
    r"\b(localStorage|sessionStorage|indexedDB)\b",
    r"document\s*\.\s*(cookie|domain|write)",
)
_ARTIFACT_DENYLIST_RE = tuple(
    re.compile(pattern, re.IGNORECASE) for pattern in ARTIFACT_HTML_DENYLIST
)

# ADR: mirrors shared/persistence/exports.py's `_EXPORT_ID` | schemas must not import that module (boto3)
_EXPORT_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}_[0-9a-f]{32}$")


def validate_artifact_html(value: str) -> str:
    """Reject artifact HTML that matches a denylisted pattern."""
    for pattern in _ARTIFACT_DENYLIST_RE:
        if pattern.search(value):
            raise ValueError(f"artifact html matches a denied pattern: {pattern.pattern}")
    return value


def _json_byte_size(value: object) -> int:
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))

ComponentId = Annotated[str, Field(min_length=1, max_length=128)]
InteractionId = Annotated[str, Field(min_length=1, max_length=128)]
ComponentText = str
MarkdownText = str
BodyText = str
SummaryText = str
TitleText = str
LabelText = str
type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ActionDescriptor(StrictModel):
    action_id: Annotated[str, Field(min_length=1, max_length=128)]
    label: Annotated[LabelText, Field(min_length=1)]
    intent: Literal["submit", "select", "approve", "reject", "open", "dismiss"] = "submit"


class ChoiceOption(StrictModel):
    label: Annotated[LabelText, Field(min_length=1)]
    value: str | int | float | bool
    description: ComponentText | None = None


class TabDescriptor(StrictModel):
    label: Annotated[LabelText, Field(min_length=1)]
    child_id: ComponentId


class CitationDescriptor(StrictModel):
    citation_id: Annotated[str, Field(min_length=1, max_length=256)]
    title: Annotated[TitleText, Field(min_length=1)]
    source_type: Annotated[str, Field(min_length=1)]
    url: str
    year: int | None = None
    relevant_passage: ComponentText | None = None

    @field_validator("url")
    @classmethod
    def validate_https_url(cls, value: str) -> str:
        return _validate_https_url(value)


class GraphSeries(StrictModel):
    name: Annotated[LabelText, Field(min_length=1)]
    values: list[int | float] = Field(max_length=MAX_GRAPH_VALUES)


class PlanStep(StrictModel):
    step_id: Annotated[str, Field(min_length=1, max_length=128)]
    title: Annotated[TitleText, Field(min_length=1)]
    description: ComponentText | None = None
    status: Literal["pending", "in_progress", "completed", "blocked"] = "pending"


class Plan(StrictModel):
    summary: Annotated[SummaryText, Field(min_length=1)]
    steps: list[PlanStep] = Field(min_length=1, max_length=MAX_PLAN_STEPS)

    @field_validator("steps")
    @classmethod
    def validate_unique_steps(cls, value: list[PlanStep]) -> list[PlanStep]:
        step_ids = [step.step_id for step in value]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("plan step IDs must be unique")
        return value


class ComponentBase(StrictModel):
    id: ComponentId


class TextComponent(ComponentBase):
    type: Literal["Text"] = "Text"
    content: ComponentText
    variant: Literal["h1", "h2", "h3", "h4", "h5", "caption", "body"] = "body"


class ImageComponent(ComponentBase):
    type: Literal["Image"] = "Image"
    src: str
    alt: ComponentText = ""
    width: PositiveInt | None = None
    height: PositiveInt | None = None

    @field_validator("src")
    @classmethod
    def validate_https_src(cls, value: str) -> str:
        return _validate_https_url(value)


class CardComponent(ComponentBase):
    type: Literal["Card"] = "Card"
    title: TitleText | None = None
    body: BodyText | None = None
    children: list[ComponentId] = Field(default_factory=list, max_length=MAX_CHILDREN)
    actions: list[ActionDescriptor] = Field(default_factory=list, max_length=MAX_ACTIONS)
    interaction_id: InteractionId | None = None

    @model_validator(mode="after")
    def validate_interaction(self) -> CardComponent:
        if self.actions and self.interaction_id is None:
            raise ValueError("interactive cards require interaction_id")
        return self


class ChoicePickerComponent(ComponentBase):
    type: Literal["ChoicePicker"] = "ChoicePicker"
    interaction_id: InteractionId
    label: LabelText
    options: list[ChoiceOption] = Field(min_length=1, max_length=MAX_OPTIONS)
    multi: bool = False
    action: ActionDescriptor

    @field_validator("options")
    @classmethod
    def validate_unique_options(cls, value: list[ChoiceOption]) -> list[ChoiceOption]:
        option_values = [(type(option.value).__name__, option.value) for option in value]
        if len(option_values) != len(set(option_values)):
            raise ValueError("choice option values must be unique")
        return value


class TextFieldComponent(ComponentBase):
    type: Literal["TextField"] = "TextField"
    interaction_id: InteractionId
    label: LabelText
    placeholder: ComponentText = ""
    multiline: bool = False
    action: ActionDescriptor


class ButtonComponent(ComponentBase):
    type: Literal["Button"] = "Button"
    interaction_id: InteractionId
    label: LabelText
    action: ActionDescriptor


class ListComponent(ComponentBase):
    type: Literal["List"] = "List"
    children: list[ComponentId] = Field(default_factory=list, max_length=MAX_CHILDREN)
    ordered: bool = False


class RowComponent(ComponentBase):
    type: Literal["Row"] = "Row"
    children: list[ComponentId] = Field(default_factory=list, max_length=MAX_CHILDREN)


class ColumnComponent(ComponentBase):
    type: Literal["Column"] = "Column"
    children: list[ComponentId] = Field(default_factory=list, max_length=MAX_CHILDREN)


class TabsComponent(ComponentBase):
    type: Literal["Tabs"] = "Tabs"
    tabs: list[TabDescriptor] = Field(min_length=1, max_length=MAX_TABS)
    active_index: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_active_index(self) -> TabsComponent:
        if self.active_index >= len(self.tabs):
            raise ValueError("active_index must identify an existing tab")
        return self


class ModalComponent(ComponentBase):
    type: Literal["Modal"] = "Modal"
    title: TitleText
    body: BodyText | None = None
    children: list[ComponentId] = Field(default_factory=list, max_length=MAX_CHILDREN)
    open: bool = True
    actions: list[ActionDescriptor] = Field(default_factory=list, max_length=MAX_ACTIONS)
    interaction_id: InteractionId | None = None

    @model_validator(mode="after")
    def validate_interaction(self) -> ModalComponent:
        if self.actions and self.interaction_id is None:
            raise ValueError("interactive modals require interaction_id")
        return self


class MarkdownComponent(ComponentBase):
    type: Literal["Markdown"] = "Markdown"
    content: MarkdownText


class GraphComponent(ComponentBase):
    type: Literal["Graph"] = "Graph"
    graph_type: Literal["line", "bar-horizontal", "bar-vertical", "scatter", "pie"] = "line"
    title: TitleText | None = None
    categories: list[str | int | float] = Field(
        default_factory=list, max_length=MAX_GRAPH_CATEGORIES
    )
    series: list[GraphSeries] = Field(min_length=1, max_length=MAX_GRAPH_SERIES)
    x_label: LabelText | None = None
    y_label: LabelText | None = None

    @model_validator(mode="after")
    def validate_total_points(self) -> GraphComponent:
        if sum(len(series.values) for series in self.series) > MAX_GRAPH_TOTAL_POINTS:
            raise ValueError(f"graph total points exceed {MAX_GRAPH_TOTAL_POINTS}")
        return self


class CitationChipComponent(ComponentBase):
    type: Literal["CitationChip"] = "CitationChip"
    citation: CitationDescriptor


class ConfidenceBadgeComponent(ComponentBase):
    type: Literal["ConfidenceBadge"] = "ConfidenceBadge"
    score: float = Field(ge=0, le=1)
    label: LabelText


class EvidencePanelComponent(ComponentBase):
    type: Literal["EvidencePanel"] = "EvidencePanel"
    citations: list[CitationDescriptor] = Field(max_length=MAX_CITATIONS)
    summary: SummaryText
    children: list[ComponentId] = Field(default_factory=list, max_length=MAX_CHILDREN)


class ReviewGateComponent(ComponentBase):
    type: Literal["ReviewGate"] = "ReviewGate"
    interaction_id: InteractionId
    plan: Plan
    actions: list[ActionDescriptor] = Field(min_length=1, max_length=MAX_ACTIONS)
    require_approval: bool = True


class TableColumn(StrictModel):
    key: Annotated[str, Field(min_length=1, max_length=64)]
    label: Annotated[LabelText, Field(min_length=1)]
    align: Literal["left", "right", "center"] = "left"
    kind: Literal["text", "number"] = "text"


class TableCellRef(StrictModel):
    row: int = Field(ge=0)
    column: int = Field(ge=0)


type TableCell = str | int | float | bool | None


class TableComponent(ComponentBase):
    type: Literal["Table"] = "Table"
    title: TitleText | None = None
    caption: ComponentText | None = None
    columns: list[TableColumn] = Field(min_length=1, max_length=MAX_TABLE_COLUMNS)
    rows: list[list[TableCell]] = Field(default_factory=list, max_length=MAX_TABLE_ROWS)
    flagged_cells: list[TableCellRef] = Field(default_factory=list, max_length=MAX_TABLE_CELLS)
    sortable: bool = False

    @model_validator(mode="after")
    def validate_shape(self) -> TableComponent:
        keys = [column.key for column in self.columns]
        if len(keys) != len(set(keys)):
            raise ValueError("table column keys must be unique")
        width = len(self.columns)
        if any(len(row) != width for row in self.rows):
            raise ValueError("every table row must have one cell per column")
        if width * len(self.rows) > MAX_TABLE_CELLS:
            raise ValueError(f"table cells exceed {MAX_TABLE_CELLS}")
        for cell in self.flagged_cells:
            if cell.row >= len(self.rows) or cell.column >= width:
                raise ValueError("flagged cells must identify existing cells")
        return self


class Metric(StrictModel):
    label: Annotated[LabelText, Field(min_length=1)]
    value: Annotated[str, Field(min_length=1, max_length=64)]
    numeric_value: float | None = None
    unit: Annotated[str, Field(max_length=32)] | None = None
    context: ComponentText | None = None
    trend: Literal["up", "down", "flat"] | None = None
    sentiment: Literal["positive", "negative", "neutral"] = "neutral"
    verified: bool = True


class StatGridComponent(ComponentBase):
    type: Literal["StatGrid"] = "StatGrid"
    title: TitleText | None = None
    metrics: list[Metric] = Field(min_length=1, max_length=MAX_METRICS)


class CalloutComponent(ComponentBase):
    type: Literal["Callout"] = "Callout"
    tone: Literal["info", "success", "warning", "danger", "neutral"] = "info"
    title: TitleText | None = None
    content: MarkdownText


class TimelineItem(StrictModel):
    item_id: Annotated[str, Field(min_length=1, max_length=128)]
    date_label: Annotated[str, Field(min_length=1, max_length=64)]
    title: Annotated[TitleText, Field(min_length=1)]
    description: ComponentText | None = None
    status: Literal["completed", "in_progress", "planned", "blocked"] | None = None


class TimelineComponent(ComponentBase):
    type: Literal["Timeline"] = "Timeline"
    title: TitleText | None = None
    items: list[TimelineItem] = Field(min_length=1, max_length=MAX_TIMELINE_ITEMS)

    @field_validator("items")
    @classmethod
    def validate_unique_items(cls, value: list[TimelineItem]) -> list[TimelineItem]:
        item_ids = [item.item_id for item in value]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("timeline item IDs must be unique")
        return value


class ComparisonCriterion(StrictModel):
    label: Annotated[LabelText, Field(min_length=1)]
    values: list[ComponentText] = Field(max_length=MAX_COMPARISON_OPTIONS)
    best_index: int | None = Field(default=None, ge=0)


class ComparisonComponent(ComponentBase):
    type: Literal["Comparison"] = "Comparison"
    title: TitleText | None = None
    options: list[Annotated[LabelText, Field(min_length=1)]] = Field(
        min_length=2, max_length=MAX_COMPARISON_OPTIONS
    )
    criteria: list[ComparisonCriterion] = Field(
        min_length=1, max_length=MAX_COMPARISON_CRITERIA
    )
    recommendation: ComponentText | None = None

    @model_validator(mode="after")
    def validate_criteria(self) -> ComparisonComponent:
        width = len(self.options)
        for criterion in self.criteria:
            if len(criterion.values) != width:
                raise ValueError("every comparison criterion must have one value per option")
            if criterion.best_index is not None and criterion.best_index >= width:
                raise ValueError("best_index must identify an existing option")
        return self


class ArtifactComponent(ComponentBase):
    type: Literal["Artifact"] = "Artifact"
    title: Annotated[TitleText, Field(min_length=1, max_length=200)]
    description: Annotated[ComponentText, Field(min_length=1)]
    html: Annotated[str, Field(min_length=1, max_length=MAX_ARTIFACT_HTML_CHARS)]
    data: JsonValue = None
    height: int = Field(
        default=ARTIFACT_DEFAULT_HEIGHT, ge=ARTIFACT_MIN_HEIGHT, le=ARTIFACT_MAX_HEIGHT
    )

    @field_validator("html")
    @classmethod
    def validate_html(cls, value: str) -> str:
        return validate_artifact_html(value)

    @field_validator("data")
    @classmethod
    def validate_data_size(cls, value: JsonValue) -> JsonValue:
        if _json_byte_size(value) > MAX_ARTIFACT_DATA_BYTES:
            raise ValueError(f"artifact data exceeds {MAX_ARTIFACT_DATA_BYTES} bytes")
        return value


class ExportFileComponent(ComponentBase):
    type: Literal["ExportFile"] = "ExportFile"
    export_id: Annotated[str, Field(min_length=1)]
    filename: Annotated[str, Field(min_length=1, max_length=255)]
    format: Literal["pdf", "docx", "md", "txt", "latex", "csv"]
    byte_size: int = Field(ge=0)

    @field_validator("export_id")
    @classmethod
    def validate_export_id(cls, value: str) -> str:
        if not _EXPORT_ID_RE.fullmatch(value):
            raise ValueError("export_id must be a valid export identifier")
        return value


Component = Annotated[
    TextComponent
    | ImageComponent
    | CardComponent
    | ChoicePickerComponent
    | TextFieldComponent
    | ButtonComponent
    | ListComponent
    | RowComponent
    | ColumnComponent
    | TabsComponent
    | ModalComponent
    | MarkdownComponent
    | GraphComponent
    | CitationChipComponent
    | ConfidenceBadgeComponent
    | EvidencePanelComponent
    | ReviewGateComponent
    | TableComponent
    | StatGridComponent
    | CalloutComponent
    | TimelineComponent
    | ComparisonComponent
    | ArtifactComponent
    | ExportFileComponent,
    Field(discriminator="type"),
]


class CEIUISurface(StrictModel):
    content_type: Literal["application/vnd.cei.ui+json"] = CEI_UI_CONTENT_TYPE
    protocol: Literal["cei-ui"] = CEI_UI_PROTOCOL
    version: Literal["1.0", "1.1"] = CEI_UI_VERSION
    surface_id: Annotated[str, Field(min_length=1, max_length=128)]
    revision: PositiveInt = 1
    root_id: ComponentId
    components: list[Component] = Field(min_length=1, max_length=MAX_COMPONENTS)

    @model_validator(mode="after")
    def validate_graph(self) -> CEIUISurface:
        components_by_id = {component.id: component for component in self.components}
        if len(components_by_id) != len(self.components):
            raise ValueError("component IDs must be unique")
        if self.root_id not in components_by_id:
            raise ValueError("root_id must identify an existing component")
        artifacts = [
            component
            for component in self.components
            if isinstance(component, ArtifactComponent)
        ]
        if len(artifacts) > MAX_ARTIFACTS_PER_SURFACE:
            raise ValueError(f"artifacts exceed {MAX_ARTIFACTS_PER_SURFACE} per surface")
        artifact_bytes = sum(
            len(artifact.html.encode("utf-8")) + _json_byte_size(artifact.data)
            for artifact in artifacts
        )
        if artifact_bytes > MAX_ARTIFACT_TOTAL_BYTES:
            raise ValueError(f"artifact payload exceeds {MAX_ARTIFACT_TOTAL_BYTES} bytes")

        references = {
            component.id: self._child_references(component) for component in self.components
        }
        missing = {
            child_id
            for child_ids in references.values()
            for child_id in child_ids
            if child_id not in components_by_id
        }
        if missing:
            raise ValueError(f"child references do not exist: {sorted(missing)}")

        visited: set[str] = set()
        active: set[str] = set()

        def visit(component_id: str, depth: int) -> None:
            if depth > MAX_COMPONENT_DEPTH:
                raise ValueError(f"component depth exceeds {MAX_COMPONENT_DEPTH}")
            if component_id in active:
                raise ValueError("component graph must not contain cycles")
            if component_id in visited:
                return
            active.add(component_id)
            for child_id in references[component_id]:
                visit(child_id, depth + 1)
            active.remove(component_id)
            visited.add(component_id)

        for component_id in components_by_id:
            visit(component_id, 1)
        return self

    @staticmethod
    def _child_references(component: Component) -> list[str]:
        if isinstance(component, TabsComponent):
            return [tab.child_id for tab in component.tabs]
        if isinstance(
            component,
            (
                CardComponent,
                ListComponent,
                RowComponent,
                ColumnComponent,
                ModalComponent,
                EvidencePanelComponent,
            ),
        ):
            return list(component.children)
        return []


class ActionRequest(StrictModel):
    request_id: Annotated[str, Field(min_length=1, max_length=128)]
    session_id: Annotated[str, Field(min_length=1, max_length=256)]
    surface_id: Annotated[str, Field(min_length=1, max_length=128)]
    revision: PositiveInt
    interaction_id: InteractionId
    component_id: ComponentId
    action_id: Annotated[str, Field(min_length=1, max_length=128)]
    value: JsonValue = None

    @field_validator("value")
    @classmethod
    def validate_value_bounds(cls, value: JsonValue) -> JsonValue:
        node_count = 0

        def visit(node: JsonValue, depth: int) -> None:
            nonlocal node_count
            if depth > MAX_ACTION_VALUE_DEPTH:
                raise ValueError(f"action value depth exceeds {MAX_ACTION_VALUE_DEPTH}")
            node_count += 1
            if node_count > MAX_ACTION_VALUE_NODES:
                raise ValueError(f"action value nodes exceed {MAX_ACTION_VALUE_NODES}")
            if isinstance(node, str):
                if len(node) > MAX_ACTION_VALUE_STRING_LENGTH:
                    raise ValueError(
                        f"action value string exceeds {MAX_ACTION_VALUE_STRING_LENGTH}"
                    )
                return
            if isinstance(node, list):
                if len(node) > MAX_ACTION_VALUE_ARRAY_ENTRIES:
                    raise ValueError(
                        f"action value array exceeds {MAX_ACTION_VALUE_ARRAY_ENTRIES} entries"
                    )
                for item in node:
                    visit(item, depth + 1)
                return
            if isinstance(node, dict):
                if len(node) > MAX_ACTION_VALUE_MAPPING_ENTRIES:
                    raise ValueError(
                        "action value mapping exceeds "
                        f"{MAX_ACTION_VALUE_MAPPING_ENTRIES} entries"
                    )
                for key, item in node.items():
                    if len(key) > MAX_ACTION_VALUE_KEY_LENGTH:
                        raise ValueError(
                            f"action value key exceeds {MAX_ACTION_VALUE_KEY_LENGTH}"
                        )
                    visit(item, depth + 1)

        visit(value, 1)
        return value


class ActionResponse(ActionRequest):
    status: Literal["accepted", "completed", "stale", "rejected", "error"]
    message: str | None = None