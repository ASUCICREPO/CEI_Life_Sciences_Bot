from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, model_validator


class SourceType(StrEnum):
    PUBMED = "pubmed"
    CLINICAL_TRIALS = "clinical_trials"
    BIORXIV = "biorxiv"
    MEDRXIV = "medrxiv"
    CHEMBL = "chembl"
    OPEN_TARGETS = "open_targets"
    CMS_COVERAGE = "cms_coverage"
    ICD10 = "icd10"
    NPI_REGISTRY = "npi_registry"
    PUBCHEM = "pubchem"
    OPENFDA = "openfda"
    PATENTSVIEW = "patentsview"
    # B44: AgentCore Web Search built-in connector -- no author/year metadata, snippet-only citation.
    WEB = "web"


class Citation(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_id: str
    source_type: SourceType
    url: str
    title: str
    # ADR: optional for SourceType.WEB only | Rationale: B44 -- the web search connector returns no
    # author/year metadata; every other source keeps these required (enforced below)
    authors: list[str] | None = None
    year: int | None = None
    relevant_passage: str
    retrieved_at: datetime
    # B44: the cited snippet text for a web search result; never the full result list.
    snippet: str | None = None

    @model_validator(mode="after")
    def _require_authors_and_year_unless_web(self) -> Citation:
        if self.source_type is not SourceType.WEB:
            if self.authors is None or self.year is None:
                raise ValueError("authors and year are required for all source types except web (B44)")
        return self
