from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from shared.schemas.citation import SourceType
from shared.state import BaseAgentState
from shared.tools.base import DataSourceResult


class TriagePlan(BaseModel):
    model_config = ConfigDict(frozen=True)
    selected_sources: list[SourceType]
    reasoning: str


class ResearchState(BaseAgentState):
    query: str
    triage_plan: TriagePlan | None
    retrieved_results: list[DataSourceResult]
    synthesis: str
