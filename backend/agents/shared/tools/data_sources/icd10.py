from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from shared.schemas.citation import SourceType
from shared.tools.base import DataSourceTool


class ICD10Tool(DataSourceTool):
    function_name = "cei-data-source-icd10"
    source_type = SourceType.ICD10
    description = "Search ICD-10 for disease and condition classification codes."

    class InputSchema(BaseModel):
        query: str
        max_results: int = Field(default=10, ge=1, le=100)

    def _build_payload(self, query: str, max_results: int = 10) -> dict[str, Any]:
        return {"query": query, "max_results": max_results}
