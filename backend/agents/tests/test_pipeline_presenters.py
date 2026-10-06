from __future__ import annotations

from shared.presenters.pipeline import build_pipeline_status_surface, build_pipeline_surface
from shared.runtime.jobs import JobRecord, JobStep
from shared.schemas.response_blocks import ProseBlock, ResponseDocument, ResponseSection


def _document() -> ResponseDocument:
    return ResponseDocument(
        title="Pipeline result",
        summary="Overall summary",
        sections=[ResponseSection(heading="Findings", blocks=[ProseBlock(kind="prose", markdown="Some findings.")])],
    )


def test_build_pipeline_surface_with_document_validates() -> None:
    step_outcomes = [
        {"step_id": "step-1", "agent": "research", "status": "completed", "text": "Research output", "document": None},
        {"step_id": "step-2", "agent": "drafting", "status": "partial", "text": "Draft output", "document": _document()},
    ]
    surface = build_pipeline_surface(
        "Final text", surface_id="pipeline-final", document=_document(), step_outcomes=step_outcomes
    )
    assert surface.surface_id == "pipeline-final"
    tab_labels = [tab.label for tab in next(c for c in surface.components if c.type == "Tabs").tabs]
    assert any("step-1" in label for label in tab_labels)
    assert any("step-2" in label for label in tab_labels)


def test_build_pipeline_surface_without_document_falls_back_to_markdown() -> None:
    step_outcomes = [{"step_id": "step-1", "agent": "research", "status": "failed", "text": "", "document": None}]
    surface = build_pipeline_surface("Final text", surface_id="pipeline-final", document=None, step_outcomes=step_outcomes)
    assert surface.root_id == "root"
    markdown_ids = [c.id for c in surface.components if c.type == "Markdown"]
    assert "summary-text" in markdown_ids
    assert "step0-markdown" in markdown_ids


def test_build_pipeline_status_surface_validates() -> None:
    record = JobRecord(
        job_id="job-1",
        session_id="s1",
        agent_type="pipeline_manager",
        steps=[
            JobStep(step_id="a", agent="research", description="x", status="completed"),
            JobStep(step_id="b", agent="drafting", description="y", status="failed", error="boom"),
        ],
        revision=3,
    )
    surface_dict = build_pipeline_status_surface(record)
    assert surface_dict["surface_id"] == "pipeline-status"
    assert surface_dict["revision"] == 3
    types = {c["type"] for c in surface_dict["components"]}
    assert {"StatGrid", "Table", "Callout", "Column"}.issubset(types)
