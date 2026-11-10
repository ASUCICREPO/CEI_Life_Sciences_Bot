from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from shared.schemas.response_blocks import ResponseDocument
from shared.state import BaseAgentState


class DocumentType(StrEnum):
    GRANT = "grant"
    VC_PITCH = "vc_pitch"
    JOURNAL = "journal"
    WHITEPAPER = "whitepaper"
    LATEX = "latex"


class Guideline(BaseModel):
    model_config = ConfigDict(frozen=True)
    source: str
    requirement: str
    url: str | None = None


class DraftingState(BaseAgentState):
    # Loaded using the authenticated actor; never supplied by the model.
    long_term_context: list[str]
    document_type: DocumentType
    guidelines: list[Guideline]
    research_questions: list[str]
    research_findings: list[dict]  # [{"question": str, "answer": str}] from the Research Agent subagent
    # B72 outline approval, modelled on analysis's scope/confirm_scope (HITL points, F13):
    outline: str
    outline_revision: int
    outline_approved: bool
    outline_correction: str | None
    # In-process callers (Pipeline dispatcher) have no interactive user or checkpointer to pause against
    skip_outline_approval: bool
    draft: str
    draft_document: ResponseDocument | None
    revision_feedback: str | None
    export_formats: list[str]
    chart_specs: list[dict]
    chart_data: dict[str, Any]
    chart_instructions: str | None
    exports: list[dict]
