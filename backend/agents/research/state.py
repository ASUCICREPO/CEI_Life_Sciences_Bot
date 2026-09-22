from __future__ import annotations

import operator
from typing import Annotated

from pydantic import BaseModel, ConfigDict

from shared.schemas.citation import SourceType
from shared.state import BaseAgentState
from shared.tools.base import DataSourceResult


class TriagePlan(BaseModel):
    model_config = ConfigDict(frozen=True)
    selected_sources: list[SourceType]
    source_queries: dict[str, str] = {}
    reasoning: str


class TraceEntry(BaseModel):
    model_config = ConfigDict(frozen=True)
    stage: str
    detail: str
    input_tokens: int = 0
    output_tokens: int = 0


class ResearchState(BaseAgentState):
    query: str
    triage_plan: TriagePlan | None
    retrieved_results: list[DataSourceResult]
    synthesis: str
    trace: Annotated[list[TraceEntry], operator.add]
