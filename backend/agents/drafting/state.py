from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

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
    document_type: DocumentType
    guidelines: list[Guideline]
    research_questions: list[str]
    research_findings: list[dict]  # [{"question": str, "answer": str}] from the Research Agent subagent
    draft: str
    revision_feedback: str | None
    export_formats: list[str]
    chart_specs: list[dict]
    chart_data: dict[str, Any]
    chart_instructions: str | None
    exports: list[dict]
