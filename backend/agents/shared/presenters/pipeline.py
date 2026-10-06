"""Deterministic presenters for the Pipeline Manager (B94, B98)."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import ValidationError

from shared.presenters.cei_ui import (
    COMPONENT_BUDGET,
    _build_confidence_callout,
    _build_document_sections,
    _metric_to_stat,
)
from shared.schemas.cei_ui import (
    CalloutComponent,
    CEIUISurface,
    ColumnComponent,
    MarkdownComponent,
    Metric,
    StatGridComponent,
    TabDescriptor,
    TableColumn,
    TableComponent,
    TabsComponent,
)
from shared.schemas.response_blocks import ResponseDocument

logger = logging.getLogger(__name__)

# Reserve one slot for the root Tabs component itself.
_ROOT_RESERVE = 1


def _step_components(step_index: int, outcome: Mapping[str, Any], remaining_budget: int) -> tuple[list[object], str]:
    document = outcome.get("document")
    text = str(outcome.get("text") or "")

    if isinstance(document, ResponseDocument):
        try:
            header = [MarkdownComponent(id=f"step{step_index}-summary", content=document.summary)]
            section_components, section_card_ids = _build_document_sections(
                document, set(), {}, reserved=len(header) + 1
            )
            total = [*header, *section_components]
            if len(total) + 1 <= remaining_budget:
                children = [header[0].id, *section_card_ids]
                column = ColumnComponent(id=f"step{step_index}-col", children=children)
                return [*total, column], column.id
        except ValidationError:
            logger.warning({"event": "pipeline_step_document_fallback", "step_index": step_index})

    markdown = MarkdownComponent(id=f"step{step_index}-markdown", content=text or "(no output)")
    return [markdown], markdown.id


def build_pipeline_surface(
    text: str,
    *,
    surface_id: str,
    document: ResponseDocument | None,
    step_outcomes: Sequence[Mapping[str, Any]],
) -> CEIUISurface:
    """Build the final pipeline surface: a summary document plus one tab per step."""
    components: list[object] = []

    if document is not None:
        try:
            header: list[object] = [MarkdownComponent(id="summary-text", content=document.summary)]
            if document.key_metrics:
                header.append(
                    StatGridComponent(
                        id="summary-metrics",
                        metrics=[_metric_to_stat(metric) for metric in document.key_metrics],
                    )
                )
            section_components, section_card_ids = _build_document_sections(
                document, set(), {}, reserved=len(header) + 2
            )
            confidence = _build_confidence_callout(document)
            summary_children = [component.id for component in header] + section_card_ids + [confidence.id]
            summary_col = ColumnComponent(id="summary-col", children=summary_children)
            components.extend(header)
            components.extend(section_components)
            components.append(confidence)
            components.append(summary_col)
            summary_child_id = summary_col.id
        except ValidationError:
            logger.warning({"event": "pipeline_summary_document_fallback"})
            components = [MarkdownComponent(id="summary-text", content=text)]
            summary_child_id = "summary-text"
    else:
        components = [MarkdownComponent(id="summary-text", content=text)]
        summary_child_id = "summary-text"

    tabs = [TabDescriptor(label="Summary", child_id=summary_child_id)]
    used = len(components)

    for index, outcome in enumerate(step_outcomes):
        remaining = max(COMPONENT_BUDGET - used - _ROOT_RESERVE - len(step_outcomes) + index, 1)
        step_components, child_id = _step_components(index, outcome, remaining)
        components.extend(step_components)
        used += len(step_components)
        step_id = str(outcome.get("step_id", f"step-{index}"))
        agent = str(outcome.get("agent", "?"))
        status = str(outcome.get("status", "idle"))
        tabs.append(TabDescriptor(label=f"{step_id} ({agent}, {status})", child_id=child_id))

    components.append(TabsComponent(id="root", tabs=tabs))
    return CEIUISurface(surface_id=surface_id, root_id="root", components=components)


_STATUS_SENTIMENT = {"failed": "negative", "partial": "negative"}


def build_pipeline_status_surface(record: Any) -> dict[str, Any]:
    """Build the live job-status surface: a StatGrid, a step Table, and failure Callouts."""
    steps = record.steps
    done = sum(1 for step in steps if step.status in ("completed", "partial"))
    running = sum(1 for step in steps if step.status == "running")
    failed = sum(1 for step in steps if step.status == "failed")

    stat_grid = StatGridComponent(
        id="status-stats",
        metrics=[
            Metric(label="Done", value=str(done), numeric_value=float(done), sentiment="positive" if done else "neutral"),
            Metric(label="Running", value=str(running), numeric_value=float(running), sentiment="neutral"),
            Metric(
                label="Failed",
                value=str(failed),
                numeric_value=float(failed),
                sentiment="negative" if failed else "neutral",
            ),
        ],
    )
    columns = [
        TableColumn(key="step", label="Step"),
        TableColumn(key="agent", label="Agent"),
        TableColumn(key="status", label="Status"),
        TableColumn(key="detail", label="Detail"),
    ]
    rows = [
        [step.step_id, step.agent, step.status, step.detail or step.error or ""]
        for step in steps
    ]
    table = TableComponent(id="status-table", columns=columns, rows=rows)

    components: list[object] = [stat_grid, table]
    children = ["status-stats", "status-table"]
    for step in steps:
        if step.status == "failed" and step.error:
            callout_id = f"callout-{step.step_id}"
            components.append(
                CalloutComponent(id=callout_id, tone="danger", title=f"{step.step_id} failed", content=step.error)
            )
            children.append(callout_id)

    components.append(ColumnComponent(id="root", children=children))
    surface = CEIUISurface(
        surface_id="pipeline-status",
        revision=max(record.revision, 1),
        root_id="root",
        components=components,
    )
    return surface.model_dump(mode="json")
