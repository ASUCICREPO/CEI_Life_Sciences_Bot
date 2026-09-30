from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from shared.schemas.citation import SourceType
from shared.schemas.response_blocks import ResponseDocument
from shared.state import BaseAgentState, TraceEntry
from shared.tools.base import DataSourceResult


class TriagePlan(BaseModel):
    model_config = ConfigDict(frozen=True)
    selected_sources: list[SourceType]
    source_queries: dict[str, str] = {}
    reasoning: str


class ResearchState(BaseAgentState):
    query: str
    triage_plan: TriagePlan | None
    retrieved_results: list[DataSourceResult]
    synthesis: str
    response_document: ResponseDocument | None
