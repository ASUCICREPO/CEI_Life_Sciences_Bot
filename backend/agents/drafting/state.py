from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from shared.state import BaseAgentState


class DocumentType(StrEnum):
    GRANT = "grant"
    VC_PITCH = "vc_pitch"
    JOURNAL = "journal"
    WHITEPAPER = "whitepaper"


class Guideline(BaseModel):
    model_config = ConfigDict(frozen=True)
    source: str
    requirement: str
    url: str | None = None


class DraftingState(BaseAgentState):
    document_type: DocumentType
    guidelines: list[Guideline]
    draft: str
    revision_feedback: str | None
