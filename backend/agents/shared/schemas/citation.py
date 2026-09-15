from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


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
