from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field

from shared.schemas.citation import Citation


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class BaseAgentOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    citations: list[Citation]
    generated_at: datetime = Field(default_factory=_utcnow)
    model_id: str | None = None


class TargetAssessment(BaseAgentOutput):
    target_name: str
    indication: str
    evidence_summary: str
    confidence_score: float
    risks: list[str]
    recommendations: list[str]


class VariantReport(BaseAgentOutput):
    variant_id: str
    gene: str
    clinical_significance: str
    population_frequency: float | None = None
    interpretation: str


class RegulatoryBrief(BaseAgentOutput):
    topic: str
    jurisdiction: str
    summary: str
    key_requirements: list[str]
    effective_date: str | None = None


class CompetitorAnalysis(BaseAgentOutput):
    company_name: str
    pipeline_stage: str
    strengths: list[str]
    weaknesses: list[str]
    market_position: str


class MarketLandscape(BaseAgentOutput):
    therapeutic_area: str
    market_size_usd: float | None = None
    growth_rate_pct: float | None = None
    key_players: list[CompetitorAnalysis]
    trends: list[str]
