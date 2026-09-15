from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from shared.schemas.citation import SourceType
from shared.tools.base import DataSourceTool


class PubMedTool(DataSourceTool):
    function_name = "cei-data-source-pubmed"
    source_type = SourceType.PUBMED
    description = "Search PubMed for biomedical literature. Returns citations with abstracts."

    class InputSchema(BaseModel):
        query: str
        max_results: int = Field(default=10, ge=1, le=100)

    def _build_payload(self, query: str, max_results: int = 10) -> dict[str, Any]:
        return {"query": query, "max_results": max_results}
