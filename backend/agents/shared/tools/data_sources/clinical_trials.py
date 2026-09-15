from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from shared.schemas.citation import SourceType
from shared.tools.base import DataSourceTool


class ClinicalTrialsTool(DataSourceTool):
    function_name = "cei-data-source-clinical-trials"
    source_type = SourceType.CLINICAL_TRIALS
    description = "Search ClinicalTrials.gov for clinical study records."

    class InputSchema(BaseModel):
        query: str
        status: str = ""
        max_results: int = Field(default=10, ge=1, le=100)

    def _build_payload(self, query: str, status: str = "", max_results: int = 10) -> dict[str, Any]:
        return {"query": query, "status": status, "max_results": max_results}
