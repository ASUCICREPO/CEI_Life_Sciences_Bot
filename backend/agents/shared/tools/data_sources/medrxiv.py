from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from shared.schemas.citation import SourceType
from shared.tools.base import DataSourceTool


class MedRxivTool(DataSourceTool):
    function_name = "cei-data-source-medrxiv"
    source_type = SourceType.MEDRXIV
    description = "Search medRxiv for health-science preprints."

    class InputSchema(BaseModel):
        query: str
        max_results: int = Field(default=10, ge=1, le=100)

    def _build_payload(self, query: str, max_results: int = 10) -> dict[str, Any]:
        return {"query": query, "max_results": max_results}
