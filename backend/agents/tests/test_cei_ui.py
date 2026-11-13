from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from shared.presenters.cei_ui import (
    MAX_RENDERED_CITATIONS,
    build_drafting_surface,
    build_final_text_surface,
    build_research_surface,
)
from shared.persistence.chat_history import ContentBlock
from shared.schemas.cei_ui import (
    ActionDescriptor,
    ActionRequest,
    ActionResponse,
    ARTIFACT_DEFAULT_HEIGHT,
    ARTIFACT_MAX_HEIGHT,
    ARTIFACT_MIN_HEIGHT,
    ArtifactComponent,
    ButtonComponent,
    CalloutComponent,
    CardComponent,
    CEI_UI_CONTENT_TYPE,
    CEI_UI_PROTOCOL,
    CEI_UI_SUPPORTED_VERSIONS,
    CEI_UI_VERSION,
    CEIUISurface,
    CitationDescriptor,
    ChoiceOption,
    ChoicePickerComponent,
    ColumnComponent,
    ComparisonComponent,
    ComparisonCriterion,
    ConfidenceBadgeComponent,
    EvidencePanelComponent,
    GraphComponent,
    GraphSeries,
    ImageComponent,
    MarkdownComponent,
    MAX_ACTION_VALUE_ARRAY_ENTRIES,
    MAX_ACTION_VALUE_DEPTH,
    MAX_ACTION_VALUE_KEY_LENGTH,
    MAX_ACTION_VALUE_MAPPING_ENTRIES,
    MAX_ACTION_VALUE_NODES,
    MAX_ACTION_VALUE_STRING_LENGTH,
    MAX_ACTIONS,
    MAX_ARTIFACT_HTML_CHARS,
    MAX_ARTIFACT_TOTAL_BYTES,
    MAX_ARTIFACTS_PER_SURFACE,
    MAX_CHILDREN,
    MAX_CITATIONS,
    MAX_COMPARISON_CRITERIA,
    MAX_COMPONENTS,
    MAX_COMPONENT_DEPTH,
    MAX_GRAPH_CATEGORIES,
    MAX_GRAPH_SERIES,
    MAX_GRAPH_TOTAL_POINTS,
    MAX_GRAPH_VALUES,
    MAX_METRICS,
    MAX_OPTIONS,
    MAX_PLAN_STEPS,
    MAX_TABLE_COLUMNS,
    MAX_TABLE_ROWS,
    MAX_TABS,
    MAX_TIMELINE_ITEMS,
    Metric,
    ModalComponent,
    Plan,
    PlanStep,
    StatGridComponent,
    TableCellRef,
    TableColumn,
    TableComponent,
    TabDescriptor,
    TabsComponent,
    TextComponent,
    TextFieldComponent,
    TimelineComponent,
    TimelineItem,
    validate_artifact_html,
)
from shared.schemas.response_blocks import (
    CalloutBlock,
    ChartBlock,
    ChartSeriesItem,
    ComparisonBlock,
    ComparisonCriterionItem,
    MetricItem,
    ProseBlock,
    ResponseDocument,
    ResponseSection,
    TableBlock,
)
from shared.tools.hitl.inline_functions import (
    AlternativeInput,
    PlanStepInput,
    build_ask_user_question_surface,
    build_get_user_input_surface,
    build_plan_approval_surface,
    build_present_alternatives_surface,
)
from shared.transport.sse import (
    EventType,
    MAX_SSE_EVENTS,
    MAX_SSE_PUBLIC_TEXT_LENGTH,
    PUBLIC_CODE_EXECUTION_STATUS,
    PUBLIC_ERROR_MESSAGE,
    PUBLIC_PROCESSING_STATUS,
    PUBLIC_TOOL_STATUS,
    SSEStream,
)

_GOLDEN_FIXTURE_PATH = (
    Path(__file__).resolve().parents[3] / "archive" / "frontend-next" / "scripts" / "fixtures" / "cei-ui-1.1-sample.json"
)


def _citation(source_id: str, url: str) -> Any:
    class Citation:
        source_type = "pubmed"
        title = f"Study {source_id}"
        year = 2026
        relevant_passage = f"Evidence for {source_id}."

    citation = Citation()
    citation.source_id = source_id
    citation.url = url
    return citation


def test_surface_uses_canonical_envelope_and_is_frozen() -> None:
    surface = build_final_text_surface("Final answer", surface_id="research-final")

    assert surface.content_type == CEI_UI_CONTENT_TYPE
    assert surface.protocol == CEI_UI_PROTOCOL
    assert surface.version == CEI_UI_VERSION
    assert surface.revision == 1
    assert surface.root_id == "root"
    with pytest.raises(ValidationError):
        surface.revision = 2
    with pytest.raises(ValidationError):
        CEIUISurface.model_validate(
            {**surface.model_dump(), "unexpected": True}
        )


@pytest.mark.parametrize(
    "components, root_id, error",
    [
        ([ColumnComponent(id="root", children=["missing"])], "root", "do not exist"),
        ([ColumnComponent(id="root", children=["root"])], "root", "cycles"),
        ([TextComponent(id="duplicate", content="one"), TextComponent(id="duplicate", content="two")], "duplicate", "unique"),
        ([TextComponent(id="only", content="one")], "missing", "root_id"),
    ],
)
def test_surface_rejects_invalid_graphs(
    components: list[object], root_id: str, error: str
) -> None:
    with pytest.raises(ValidationError, match=error):
        CEIUISurface(surface_id="invalid", root_id=root_id, components=components)


def test_surface_rejects_graphs_beyond_depth_limit() -> None:
    components = [
        ColumnComponent(id=f"node-{index}", children=[f"node-{index + 1}"])
        for index in range(MAX_COMPONENT_DEPTH)
    ]
    components.append(TextComponent(id=f"node-{MAX_COMPONENT_DEPTH}", content="end"))

    with pytest.raises(ValidationError, match="depth"):
        CEIUISurface(surface_id="deep", root_id="node-0", components=components)


def test_surface_rejects_too_many_components() -> None:
    components = [
        TextComponent(id=f"text-{index}", content="text")
        for index in range(MAX_COMPONENTS + 1)
    ]

    with pytest.raises(ValidationError):
        CEIUISurface(surface_id="large", root_id="text-0", components=components)


def test_visible_component_content_accepts_long_text() -> None:
    text = "x" * 100_001
    action = ActionDescriptor(action_id="submit", label=text)
    citation = CitationDescriptor(
        citation_id="citation",
        title=text,
        source_type=text,
        url="https://example.test",
        relevant_passage=text,
    )

    assert TextComponent(id="text", content=text).content == text
    assert MarkdownComponent(id="markdown", content=text).content == text
    image = ImageComponent(id="image", src="https://example.test/image.png", alt=text)
    card = CardComponent(
        id="card", title=text, body=text, actions=[action], interaction_id="card"
    )
    modal = ModalComponent(
        id="modal", title=text, body=text, actions=[action], interaction_id="modal"
    )
    choice_picker = ChoicePickerComponent(
        id="choice",
        interaction_id="choice",
        label=text,
        options=[ChoiceOption(label=text, value="one", description=text)],
        action=action,
    )
    text_field = TextFieldComponent(
        id="field", interaction_id="field", label=text, placeholder=text, action=action
    )
    button = ButtonComponent(
        id="button", interaction_id="button", label=text, action=action
    )
    tab = TabDescriptor(label=text, child_id="child")
    graph = GraphComponent(
        id="graph",
        title=text,
        categories=[text],
        series=[GraphSeries(name=text, values=[1])],
        x_label=text,
        y_label=text,
    )

    assert image.alt == text
    assert card.title == text and card.body == text
    assert modal.title == text and modal.body == text
    assert choice_picker.label == text and choice_picker.options[0].description == text
    assert text_field.label == text and text_field.placeholder == text
    assert button.label == text
    assert tab.label == text
    assert graph.categories == [text] and graph.series[0].name == text
    assert ConfidenceBadgeComponent(id="confidence", score=1, label=text).label == text
    assert EvidencePanelComponent(id="evidence", citations=[citation], summary=text).summary == text
    assert Plan(summary=text, steps=[PlanStep(step_id="step", title=text, description=text)]).summary == text
    assert ActionResponse(
        request_id="request",
        session_id="session",
        surface_id="surface",
        revision=1,
        interaction_id="interaction",
        component_id="component",
        action_id="submit",
        status="completed",
        message=text,
    ).message == text


def test_component_collection_bounds() -> None:
    action = ActionDescriptor(action_id="submit", label="Submit")
    citation = CitationDescriptor(
        citation_id="citation", title="Study", source_type="pubmed", url="https://example.test"
    )
    step = PlanStep(step_id="step", title="Step")
    tab = TabDescriptor(label="Tab", child_id="child")
    option = ChoiceOption(label="Option", value="option")

    bounded_collections = [
        (ColumnComponent, {"id": "column", "children": ["child"] * (MAX_CHILDREN + 1)}),
        (
            CardComponent,
            {
                "id": "card",
                "interaction_id": "card-action",
                "actions": [action] * (MAX_ACTIONS + 1),
            },
        ),
        (TabsComponent, {"id": "tabs", "tabs": [tab] * (MAX_TABS + 1)}),
        (
            ChoicePickerComponent,
            {
                "id": "choice",
                "interaction_id": "choice-action",
                "label": "Choose",
                "options": [option] * (MAX_OPTIONS + 1),
                "action": action,
            },
        ),
        (
            EvidencePanelComponent,
            {
                "id": "evidence",
                "citations": [citation] * (MAX_CITATIONS + 1),
                "summary": "Summary",
            },
        ),
        (
            Plan,
            {"summary": "Plan", "steps": [step] * (MAX_PLAN_STEPS + 1)},
        ),
    ]

    for model, values in bounded_collections:
        with pytest.raises(ValidationError):
            model(**values)


def test_graph_collection_and_total_point_bounds() -> None:
    with pytest.raises(ValidationError):
        GraphComponent(
            id="graph",
            categories=list(range(MAX_GRAPH_CATEGORIES + 1)),
            series=[GraphSeries(name="Series", values=[1])],
        )
    with pytest.raises(ValidationError):
        GraphComponent(
            id="graph",
            series=[GraphSeries(name=f"Series {index}", values=[1]) for index in range(MAX_GRAPH_SERIES + 1)],
        )
    with pytest.raises(ValidationError):
        GraphSeries(name="Series", values=[1] * (MAX_GRAPH_VALUES + 1))

    points_per_series = MAX_GRAPH_TOTAL_POINTS // (MAX_GRAPH_SERIES - 1) + 1
    with pytest.raises(ValidationError, match="total points"):
        GraphComponent(
            id="graph",
            series=[
                GraphSeries(name=f"Series {index}", values=[1] * points_per_series)
                for index in range(MAX_GRAPH_SERIES - 1)
            ],
        )


def test_component_constraints_reject_unsafe_or_ambiguous_values() -> None:
    action = ActionDescriptor(action_id="submit", label="Submit")
    with pytest.raises(ValidationError, match="HTTPS"):
        ImageComponent(id="image", src="http://example.test/image.png")
    with pytest.raises(ValidationError):
        ConfidenceBadgeComponent(id="confidence", score=1.1, label="High")
    with pytest.raises(ValidationError, match="unique"):
        ChoicePickerComponent(
            id="choice",
            interaction_id="choice-1",
            label="Choose",
            options=[ChoiceOption(label="One", value="same"), ChoiceOption(label="Two", value="same")],
            action=action,
        )
    with pytest.raises(ValidationError, match="interaction_id"):
        ChoicePickerComponent.model_validate(
            {
                "id": "choice",
                "label": "Choose",
                "options": [{"label": "One", "value": "one"}],
                "action": {"action_id": "submit", "label": "Submit"},
            }
        )


def test_action_request_and_response_support_typed_json_values() -> None:
    request = ActionRequest(
        request_id="request-1",
        session_id="session-1",
        surface_id="surface-1",
        revision=1,
        interaction_id="interaction-1",
        component_id="choice",
        action_id="submit",
        value={"selected": ["one", 2, True]},
    )
    response = ActionResponse(**request.model_dump(), status="completed")

    assert response.status == "completed"
    with pytest.raises(ValidationError):
        ActionResponse(**request.model_dump(), status="unknown")


@pytest.mark.parametrize(
    "value",
    [None, "text", 1, 1.5, True, ["one", 2, False], {"selected": ["one"]}],
)
def test_action_request_preserves_valid_json_values(value: Any) -> None:
    request = ActionRequest(
        request_id="request-1",
        session_id="session-1",
        surface_id="surface-1",
        revision=1,
        interaction_id="interaction-1",
        component_id="choice",
        action_id="submit",
        value=value,
    )

    assert request.value == value


def test_action_request_rejects_recursive_value_limits() -> None:
    base = {
        "request_id": "request-1",
        "session_id": "session-1",
        "surface_id": "surface-1",
        "revision": 1,
        "interaction_id": "interaction-1",
        "component_id": "choice",
        "action_id": "submit",
    }
    too_deep: Any = "value"
    for _ in range(MAX_ACTION_VALUE_DEPTH):
        too_deep = [too_deep]
    too_many_nodes = [[index] * 10 for index in range(100)]
    invalid_values = [
        too_deep,
        too_many_nodes,
        list(range(MAX_ACTION_VALUE_ARRAY_ENTRIES + 1)),
        {str(index): index for index in range(MAX_ACTION_VALUE_MAPPING_ENTRIES + 1)},
        {"k" * (MAX_ACTION_VALUE_KEY_LENGTH + 1): "value"},
        "x" * (MAX_ACTION_VALUE_STRING_LENGTH + 1),
    ]

    for value in invalid_values:
        with pytest.raises(ValidationError):
            ActionRequest(**base, value=value)


def test_research_surface_uses_domain_evidence_and_caps_citations() -> None:
    citations = [_citation(f"PMID:{index}", f"https://example.test/{index}") for index in range(14)]
    citations.insert(3, _citation("PMID:2", "https://example.test/2"))

    class Result:
        source_type = "pubmed"

    result = Result()
    result.citations = citations
    surface = build_research_surface(
        "Answer", surface_id="research-final", retrieved_results=[result]
    )

    components = {component.id: component for component in surface.components}
    panel = components["evidence-panel"]
    assert panel.type == "EvidencePanel"
    assert panel.summary == "Showing 12 of 14 citations"
    assert len(panel.citations) == MAX_RENDERED_CITATIONS
    assert panel.citations[2].citation_id == "PMID:2"
    assert panel.citations[3].citation_id == "PMID:3"
    assert sum(component.type == "CitationChip" for component in surface.components) == 12


def test_research_fallback_is_a_complete_surface() -> None:
    surface = build_research_surface(
        "No response generated.", surface_id="research-final", retrieved_results=[]
    )

    assert surface.root_id == "root"
    panel = next(component for component in surface.components if component.id == "evidence-panel")
    assert panel.summary == "Showing 0 of 0 citations"


def test_drafting_surface_filters_private_export_and_chart_fields() -> None:
    """The completed-turn surface is a short summary + Files list: no full draft text, no raw
    chart/export fields, one `ExportFile` component per export (B1xx #3)."""
    export_id = "6f1c2d3e-0000-4000-8000-000000000001_" + "a" * 32
    surface = build_drafting_surface(
        "Draft complete.",
        surface_id="drafting-final",
        exports=[
            {
                "export_id": export_id,
                "format": "pdf",
                "filename": "brief.pdf",
                "byte_size": 42,
                "local_path": "C:/private/brief.pdf",
            }
        ],
        chart_specs=[
            {
                "title": "Evidence by year",
                "chart_type": "bar",
                "x_label": "Year",
                "y_label": "Studies",
                "section": "Evidence",
                "data": {"x": [2026], "studies": [12]},
                "local_path": "C:/private/chart.png",
            }
        ],
        chart_instructions="Provide 2027 evidence data.",
    )

    payload = json.dumps(surface.model_dump(mode="json"))
    export_files = [c for c in surface.components if c.type == "ExportFile"]
    assert len(export_files) == 1
    assert export_files[0].filename == "brief.pdf"
    assert export_files[0].export_id == export_id
    assert "brief.pdf" in payload
    assert "Evidence by year" in payload
    assert "Provide 2027 evidence data." in payload
    assert "local_path" not in payload
    assert "C:/private" not in payload
    assert '"data"' not in payload


def test_hitl_tools_return_complete_interactive_surfaces() -> None:
    payloads = [
        build_ask_user_question_surface("Choose", ["One", "Two"]),
        build_present_alternatives_surface(
            "Options",
            [
                AlternativeInput(title="One", description="First"),
                AlternativeInput(title="Two", description="Second"),
            ],
        ),
        build_plan_approval_surface(
            "Plan", [PlanStepInput(title="Search"), PlanStepInput(title="Draft")]
        ),
        build_get_user_input_surface("Describe the target"),
    ]

    surfaces = [CEIUISurface.model_validate(payload) for payload in payloads]
    assert [surface.surface_id for surface in surfaces] == [
        "hitl-ask-user-question",
        "hitl-present-alternatives",
        "hitl-plan-approval",
        "hitl-get-user-input",
    ]
    assert all(
        any(getattr(component, "interaction_id", None) for component in surface.components)
        for surface in surfaces
    )


def test_sse_emits_typed_ui_surface_event() -> None:
    stream = SSEStream()
    surface = build_final_text_surface("Answer", surface_id="final")

    stream.emit_ui_surface(surface)

    assert stream.events[0].event == EventType.UI_SURFACE
    assert stream.events[0].data == surface
    assert "event: ui_surface" in stream.serialize()
    assert CEI_UI_CONTENT_TYPE in stream.serialize()


def test_sse_redacts_private_payloads_and_bounds_public_text() -> None:
    stream = SSEStream()

    stream.emit_thinking("private reasoning")
    stream.emit_tool_use("private_tool", {"secret": "private input"})
    stream.emit_code_execution("print('private code')", {"stdout": "private result"})
    stream.emit_error("private traceback", code="private_error_code")
    stream.emit_text("x" * (MAX_SSE_PUBLIC_TEXT_LENGTH + 1))

    assert stream.events[0].data == {"status": PUBLIC_PROCESSING_STATUS}
    assert stream.events[1].data == {"status": PUBLIC_TOOL_STATUS}
    assert stream.events[2].data == {"status": PUBLIC_CODE_EXECUTION_STATUS}
    assert stream.events[3].data == {
        "message": PUBLIC_ERROR_MESSAGE,
        "code": "internal_error",
    }
    assert len(stream.events[4].data["text"]) == MAX_SSE_PUBLIC_TEXT_LENGTH
    serialized = stream.serialize()
    for private_value in (
        "private reasoning",
        "private_tool",
        "private input",
        "private code",
        "private result",
        "private traceback",
        "private_error_code",
    ):
        assert private_value not in serialized


def test_sse_accepts_only_allowlisted_error_codes() -> None:
    stream = SSEStream()

    stream.emit_error("private detail", code="rate_limited")

    assert stream.events[0].data == {
        "message": PUBLIC_ERROR_MESSAGE,
        "code": "rate_limited",
    }


def test_sse_rejects_events_beyond_limit() -> None:
    stream = SSEStream()
    for _ in range(MAX_SSE_EVENTS):
        stream.emit_done()

    with pytest.raises(ValueError, match="event count"):
        stream.emit_done()
    assert len(stream.events) == MAX_SSE_EVENTS


def test_chat_history_accepts_valid_ui_and_legacy_a2ui_reads() -> None:
    surface = build_final_text_surface("Answer", surface_id="final")

    ui_block = ContentBlock(block_type="ui", body=surface.model_dump(mode="json"))
    legacy_block = ContentBlock(block_type="a2ui", body={"legacy": True})

    assert ui_block.block_type == "ui"
    assert legacy_block.block_type == "a2ui"
    with pytest.raises(ValidationError):
        ContentBlock(block_type="ui", body={"legacy": True})


# ── CEI UI 1.1 component bounds ──────────────────────────────────────────────


def test_1_0_surface_still_validates() -> None:
    surface = CEIUISurface(
        content_type=CEI_UI_CONTENT_TYPE,
        protocol=CEI_UI_PROTOCOL,
        version="1.0",
        surface_id="legacy",
        root_id="root",
        components=[MarkdownComponent(id="root", content="Hello")],
    )

    assert surface.version == "1.0"
    assert "1.0" in CEI_UI_SUPPORTED_VERSIONS


def test_golden_fixture_validates() -> None:
    payload = json.loads(_GOLDEN_FIXTURE_PATH.read_text(encoding="utf-8"))

    surface = CEIUISurface.model_validate(payload)

    assert surface.version == "1.1"


def test_table_component_bounds() -> None:
    column = TableColumn(key="c0", label="Col")
    with pytest.raises(ValidationError):
        TableComponent(id="t", columns=[column] * (MAX_TABLE_COLUMNS + 1))
    with pytest.raises(ValidationError):
        TableComponent(id="t", columns=[column], rows=[["x"]] * (MAX_TABLE_ROWS + 1))
    many_columns = [TableColumn(key=f"c{i}", label=f"C{i}") for i in range(MAX_TABLE_COLUMNS)]
    with pytest.raises(ValidationError, match="cells exceed"):
        TableComponent(
            id="t",
            columns=many_columns,
            rows=[["x"] * MAX_TABLE_COLUMNS for _ in range(MAX_TABLE_ROWS)],
        )


def test_table_component_rejects_ragged_rows() -> None:
    with pytest.raises(ValidationError, match="one cell per column"):
        TableComponent(
            id="t",
            columns=[TableColumn(key="c0", label="A"), TableColumn(key="c1", label="B")],
            rows=[["only-one"]],
        )


def test_table_component_rejects_out_of_range_flagged_cells() -> None:
    with pytest.raises(ValidationError, match="existing cells"):
        TableComponent(
            id="t",
            columns=[TableColumn(key="c0", label="A")],
            rows=[["x"]],
            flagged_cells=[TableCellRef(row=5, column=0)],
        )


def test_callout_component_rejects_unknown_tone() -> None:
    CalloutComponent(id="c", tone="warning", content="Careful.")
    with pytest.raises(ValidationError):
        CalloutComponent(id="c", tone="urgent", content="Careful.")


def test_stat_grid_component_bounds() -> None:
    metric = Metric(label="Metric", value="1")
    with pytest.raises(ValidationError):
        StatGridComponent(id="grid", metrics=[metric] * (MAX_METRICS + 1))
    with pytest.raises(ValidationError):
        StatGridComponent(id="grid", metrics=[])


def test_timeline_component_bounds_and_unique_ids() -> None:
    item = TimelineItem(item_id="e0", date_label="2021", title="Event")
    with pytest.raises(ValidationError):
        TimelineComponent(id="tl", items=[item] * (MAX_TIMELINE_ITEMS + 1))
    with pytest.raises(ValidationError, match="unique"):
        TimelineComponent(
            id="tl",
            items=[
                TimelineItem(item_id="dup", date_label="2021", title="One"),
                TimelineItem(item_id="dup", date_label="2022", title="Two"),
            ],
        )


def test_comparison_component_bounds() -> None:
    with pytest.raises(ValidationError):
        ComparisonComponent(
            id="cmp",
            options=["Only one"],
            criteria=[ComparisonCriterion(label="Dosing", values=["Weekly"])],
        )
    with pytest.raises(ValidationError, match="one value per option"):
        ComparisonComponent(
            id="cmp",
            options=["A", "B"],
            criteria=[ComparisonCriterion(label="Dosing", values=["Weekly"])],
        )
    with pytest.raises(ValidationError, match="best_index"):
        ComparisonComponent(
            id="cmp",
            options=["A", "B"],
            criteria=[ComparisonCriterion(label="Dosing", values=["Weekly", "Daily"], best_index=5)],
        )
    with pytest.raises(ValidationError):
        ComparisonComponent(
            id="cmp",
            options=["A", "B"],
            criteria=[ComparisonCriterion(label=f"C{i}", values=["x", "y"]) for i in range(MAX_COMPARISON_CRITERIA + 1)],
        )


_BENIGN_ARTIFACT_HTML = (
    "<svg width='10' height='10'><rect width='10' height='10'/></svg>"
    "<canvas id='c'></canvas>"
    "<script>document.getElementById('c').addEventListener('click', () => {});</script>"
)


@pytest.mark.parametrize(
    "html",
    [
        "<script>fetch('/x')</script>",
        "<a href='https://example.test'>link</a>",
        "<iframe src='about:blank'></iframe>",
        "<script>top.location = 'x'</script>",
        "<script>localStorage.setItem('a', 'b')</script>",
        "<script>window.postMessage('x')</script>",
    ],
)
def test_artifact_html_denylist_rejects_unsafe_patterns(html: str) -> None:
    with pytest.raises(ValueError):
        validate_artifact_html(html)
    with pytest.raises(ValidationError):
        ArtifactComponent(id="a", title="Bad", description="desc", html=html)


def test_artifact_html_denylist_accepts_benign_svg_canvas_and_events() -> None:
    assert validate_artifact_html(_BENIGN_ARTIFACT_HTML) == _BENIGN_ARTIFACT_HTML
    artifact = ArtifactComponent(id="a", title="Good", description="desc", html=_BENIGN_ARTIFACT_HTML)
    assert artifact.html == _BENIGN_ARTIFACT_HTML


def test_artifact_component_height_bounds() -> None:
    with pytest.raises(ValidationError):
        ArtifactComponent(
            id="a", title="T", description="d", html=_BENIGN_ARTIFACT_HTML, height=ARTIFACT_MIN_HEIGHT - 1
        )
    with pytest.raises(ValidationError):
        ArtifactComponent(
            id="a", title="T", description="d", html=_BENIGN_ARTIFACT_HTML, height=ARTIFACT_MAX_HEIGHT + 1
        )
    artifact = ArtifactComponent(id="a", title="T", description="d", html=_BENIGN_ARTIFACT_HTML)
    assert artifact.height == ARTIFACT_DEFAULT_HEIGHT


def test_artifact_component_data_size_bound() -> None:
    big_rows = [["x" * 100] for _ in range(2_000)]
    with pytest.raises(ValidationError, match="artifact data exceeds"):
        ArtifactComponent(
            id="a",
            title="T",
            description="d",
            html=_BENIGN_ARTIFACT_HTML,
            data={"rows": big_rows},
        )


def test_surface_rejects_more_than_four_artifacts() -> None:
    artifacts = [
        ArtifactComponent(id=f"a{i}", title=f"T{i}", description="d", html=_BENIGN_ARTIFACT_HTML)
        for i in range(MAX_ARTIFACTS_PER_SURFACE + 1)
    ]
    with pytest.raises(ValidationError, match="artifacts exceed"):
        CEIUISurface(
            surface_id="s",
            root_id="a0",
            components=artifacts,
        )


def test_surface_rejects_total_artifact_bytes_over_budget() -> None:
    html = "x" * MAX_ARTIFACT_HTML_CHARS
    artifacts = [
        ArtifactComponent(id=f"a{i}", title=f"T{i}", description="d", html=html, data={"n": i})
        for i in range(MAX_ARTIFACTS_PER_SURFACE)
    ]
    with pytest.raises(ValidationError, match="artifact payload exceeds"):
        CEIUISurface(surface_id="s", root_id="a0", components=artifacts)


# ── Research and drafting presenters with a ResponseDocument ────────────────


def _citation_result(source_id: str, url: str) -> Any:
    class Citation:
        source_type = "pubmed"
        title = f"Study {source_id}"
        year = 2026
        relevant_passage = f"Evidence for {source_id}."

    citation = Citation()
    citation.source_id = source_id
    citation.url = url

    class Result:
        source_type = "pubmed"

    result = Result()
    result.citations = [citation]
    return result


def _rich_document() -> ResponseDocument:
    return ResponseDocument(
        title="GLP-1 agonists: Phase 3 efficacy",
        summary="Semaglutide gives the largest weight loss [PMID:111].",
        key_metrics=[
            MetricItem(label="Mean weight loss", value=14.9, unit="%", source_ids=["PMID:111"]),
        ],
        sections=[
            ResponseSection(
                heading="Efficacy",
                blocks=[
                    ProseBlock(kind="prose", markdown="Semaglutide beat liraglutide [PMID:111]."),
                    TableBlock(
                        kind="table",
                        columns=["Agent", "Weight loss (%)"],
                        rows=[["Semaglutide", 14.9], ["Liraglutide", 8.0]],
                        source_ids=["PMID:111"],
                        unverified_cells=[(1, 1)],
                    ),
                    ChartBlock(
                        kind="chart",
                        title="Weight loss by agent",
                        categories=["Semaglutide", "Liraglutide"],
                        series=[ChartSeriesItem(name="Weight loss", values=[14.9, 8.0])],
                        source_ids=["PMID:111"],
                    ),
                    CalloutBlock(kind="callout", tone="info", markdown="Two trials only [PMID:111] [PMID:UNKNOWN]."),
                ],
            )
        ],
        confidence="medium",
        confidence_rationale="Two trials only.",
        open_questions=["What is the long-term durability?"],
    )


def test_research_presenter_with_document_produces_rich_components() -> None:
    surface = build_research_surface(
        "unused",
        surface_id="research-final",
        retrieved_results=[_citation_result("PMID:111", "https://example.test/111")],
        document=_rich_document(),
    )

    types = {component.type for component in surface.components}
    assert {"StatGrid", "Table", "Graph", "Card", "Callout"}.issubset(types)


def test_research_presenter_chips_only_reference_known_citation_ids() -> None:
    surface = build_research_surface(
        "unused",
        surface_id="research-final",
        retrieved_results=[_citation_result("PMID:111", "https://example.test/111")],
        document=_rich_document(),
    )

    chips = [c for c in surface.components if c.type == "CitationChip"]
    chip_ids = {chip.citation.citation_id for chip in chips}
    assert "PMID:111" in chip_ids
    assert "PMID:UNKNOWN" not in chip_ids


def test_research_presenter_collapses_sections_beyond_budget() -> None:
    section = ResponseSection(
        heading="Section",
        blocks=[
            ChartBlock(
                kind="chart",
                title=f"Chart {i}",
                categories=["A"],
                series=[ChartSeriesItem(name="S", values=[1.0])],
                basis="estimate",
            )
            for i in range(10)
        ],
    )
    for block in section.blocks:
        block.verification = "estimate"
    document = ResponseDocument(
        title="Stress",
        summary="Summary.",
        sections=[section.model_copy(deep=True) for _ in range(12)],
    )

    surface = build_research_surface(
        "unused", surface_id="research-final", retrieved_results=[], document=document
    )

    graph_components = [c for c in surface.components if c.type == "Graph"]
    section_cards = [c for c in surface.components if c.type == "Card" and c.id.startswith("s")]
    assert len(surface.components) <= MAX_COMPONENTS
    assert len(graph_components) < 120
    assert len(section_cards) == 12


def test_research_presenter_falls_back_to_legacy_on_invalid_document() -> None:
    bad_section = ResponseSection(
        heading="Bad",
        blocks=[
            ComparisonBlock(
                kind="comparison",
                options=["A", "B"],
                criteria=[ComparisonCriterionItem(label="X", values=["only-one"])],
            )
        ],
    )
    document = ResponseDocument(title="Broken", summary="Summary.", sections=[bad_section])

    surface = build_research_surface(
        "Fallback text", surface_id="research-final", retrieved_results=[], document=document
    )

    assert surface.root_id == "root"
    assert any(component.type == "Markdown" for component in surface.components)


def test_drafting_presenter_notes_charts_without_leaking_raw_fields() -> None:
    """Chart specs become a short note (title only), never a `Graph` component or raw data (B1xx #3)."""
    surface = build_drafting_surface(
        "Draft complete.",
        surface_id="drafting-final",
        exports=[],
        chart_specs=[
            {
                "title": "Trend",
                "chart_type": "line",
                "x_label": "Year",
                "y_label": "Count",
                "section": "",
                "data": {"x": [2025, 2026], "Series A": [1, 2]},
                "local_path": "/tmp/x.png",
            }
        ],
        chart_instructions=None,
    )

    payload = json.dumps(surface.model_dump(mode="json"))
    graphs = [c for c in surface.components if c.type == "Graph"]

    assert len(graphs) == 0
    assert "Trend" in payload
    assert "local_path" not in payload
    assert "base64" not in payload
    assert '"data"' not in payload


def test_legacy_markdown_splits_into_section_cards() -> None:
    text = (
        "Intro paragraph.\n\n"
        "## Efficacy\n"
        "Some content [PMID:1].\n\n"
        "## Safety\n"
        "More content."
    )

    surface = build_research_surface(text, surface_id="research-final", retrieved_results=[])

    card_titles = {c.title for c in surface.components if c.type == "Card"}
    assert {"Overview", "Efficacy", "Safety"}.issubset(card_titles)