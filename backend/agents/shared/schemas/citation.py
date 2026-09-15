from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator


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


class Citation(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_id: str
    source_type: SourceType
    url: str
    title: str
    authors: list[str]
    year: int
    relevant_passage: str
    retrieved_at: datetime

    @field_validator("source_type", mode="before")
    @classmethod
    def _validate_source_type(cls, value: str) -> SourceType:
        if isinstance(value, SourceType):
            return value
        try:
            return SourceType(value)
        except ValueError:
            allowed = ", ".join(member.value for member in SourceType)
            raise ValueError(
                f"Unknown source type '{value}'. Allowed: {allowed}"
            )
