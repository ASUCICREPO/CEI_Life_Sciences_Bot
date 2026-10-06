from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from shared.schemas.response_blocks import ResponseDocument
from shared.state import BaseAgentState
from shared.tools.base import DataSourceResult


class AnalysisType(StrEnum):
    COMPETITOR = "competitor"
    MARKET = "market"
    LEGAL_LANDSCAPE = "legal_landscape"


class Comparison(BaseModel):
    model_config = ConfigDict(frozen=True)
    dimension: str
    findings: list[str]
    sources: list[str]


class ScopeProposal(BaseModel):
    model_config = ConfigDict(frozen=True)
    analysis_type: AnalysisType
    competitors: list[str] = []
    market: str | None = None
    jurisdiction: str | None = None
    focal_entity: str | None = None


class GatherQueries(BaseModel):
    model_config = ConfigDict(frozen=True)
    source_queries: dict[str, str] = {}


class ComparisonsOutput(BaseModel):
    model_config = ConfigDict(frozen=True)
    comparisons: list[Comparison]


class AnalysisState(BaseAgentState):
    query: str
    analysis_type: AnalysisType | None
    competitors: list[str]
    market: str | None
    jurisdiction: str | None
    focal_entity: str | None
    confirmed_scope: bool
    scope_revision: int
    scope_correction: str | None
    retrieved_results: list[DataSourceResult]
    findings: list[str]
    comparisons: list[Comparison]
    report: str
    response_document: ResponseDocument | None
