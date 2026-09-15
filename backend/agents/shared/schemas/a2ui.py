from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class A2UIComponent(BaseModel):
    model_config = ConfigDict(frozen=True)

    component_type: str

    def to_dict(self) -> dict[str, Any]:
        fields = self.model_dump(exclude={"component_type"})
        return {"type": self.component_type, "props": fields}


class TextComponent(A2UIComponent):
    component_type: Literal["Text"] = "Text"
    content: str


class ImageComponent(A2UIComponent):
    component_type: Literal["Image"] = "Image"
    src: str
    alt: str = ""
    width: int | None = None
    height: int | None = None


class CardComponent(A2UIComponent):
    component_type: Literal["Card"] = "Card"
    title: str
    body: str
    actions: list[dict[str, Any]] | None = None


class ChoicePickerComponent(A2UIComponent):
    component_type: Literal["ChoicePicker"] = "ChoicePicker"
    label: str
    options: list[dict[str, str]]
    multi: bool = False


class TextFieldComponent(A2UIComponent):
    component_type: Literal["TextField"] = "TextField"
    label: str
    placeholder: str = ""


class ButtonComponent(A2UIComponent):
    component_type: Literal["Button"] = "Button"
    label: str
    action: str


class ListComponent(A2UIComponent):
    component_type: Literal["List"] = "List"
    items: list[dict[str, Any]]
    ordered: bool = False


class RowComponent(A2UIComponent):
    component_type: Literal["Row"] = "Row"
    children: list[dict[str, Any]]


class ColumnComponent(A2UIComponent):
    component_type: Literal["Column"] = "Column"
    children: list[dict[str, Any]]


class TabsComponent(A2UIComponent):
    component_type: Literal["Tabs"] = "Tabs"
    tabs: list[dict[str, Any]]
    active_index: int = 0


class ModalComponent(A2UIComponent):
    component_type: Literal["Modal"] = "Modal"
    title: str
    body: str
    open: bool = True
    actions: list[dict[str, Any]] | None = None


class MarkdownComponent(A2UIComponent):
    component_type: Literal["Markdown"] = "Markdown"
    content: str


class GraphComponent(A2UIComponent):
    component_type: Literal["Graph"] = "Graph"
    data: dict[str, Any]
    layout: dict[str, Any] | None = None
    graph_type: Literal["line", "bar-horizontal", "bar-vertical", "scatter", "pie"] = "line"


class CitationChipComponent(A2UIComponent):
    component_type: Literal["CitationChip"] = "CitationChip"
    citation_id: str
    title: str
    source_type: str


class ConfidenceBadgeComponent(A2UIComponent):
    component_type: Literal["ConfidenceBadge"] = "ConfidenceBadge"
    score: float
    label: str


class EvidencePanelComponent(A2UIComponent):
    component_type: Literal["EvidencePanel"] = "EvidencePanel"
    citations: list[dict[str, Any]]
    summary: str


class ReviewGateComponent(A2UIComponent):
    component_type: Literal["ReviewGate"] = "ReviewGate"
    plan: dict[str, Any]
    require_approval: bool = True


class A2UIContainer(BaseModel):
    model_config = ConfigDict(frozen=True)

    components: list[A2UIComponent]

    def to_payload(self) -> dict[str, Any]:
        return {
            "content_type": "application/a2ui+json",
            "components": [c.to_dict() for c in self.components],
        }
