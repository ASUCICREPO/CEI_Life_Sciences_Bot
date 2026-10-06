from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from shared.schemas.response_blocks import ResponseDocument
from shared.state import BaseAgentState


class StepStatusValue(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


class PipelineStep(BaseModel):
    model_config = ConfigDict(frozen=True)
    step_id: str
    agent: str
    description: str
    dependencies: list[str] = []
    analysis_type: str | None = None
    scope: dict[str, Any] | None = None
    document_type: str | None = None


class PipelinePlan(BaseModel):
    model_config = ConfigDict(frozen=True)
    summary: str
    steps: list[PipelineStep]
    estimated_duration_seconds: int


class StepStatus(BaseModel):
    step_id: str
    agent: str
    status: StepStatusValue = StepStatusValue.IDLE
    result: dict[str, Any] | None = None
    error: str | None = None


class PipelineState(BaseAgentState):
    plan: PipelinePlan | None
    plan_approved: bool
    steps: list[StepStatus]
    synthesis: str
    retry_count: int
    # ADR: added for the MVP presenter | synthesize needs a structured document, not just prose (B94)
    response_document: ResponseDocument | None
