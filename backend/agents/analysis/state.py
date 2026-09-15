from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

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


class AnalysisState(BaseAgentState):
    analysis_type: AnalysisType
    gathered_data: list[DataSourceResult]
    comparisons: list[Comparison]
    report: str
