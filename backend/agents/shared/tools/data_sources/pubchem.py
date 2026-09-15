from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from shared.schemas.citation import SourceType
from shared.tools.base import DataSourceTool


class PubChemTool(DataSourceTool):
    function_name = "cei-data-source-pubchem"
    source_type = SourceType.PUBCHEM
    description = "Search PubChem for chemical compound data including structures and bioactivity."

    class InputSchema(BaseModel):
        query: str
        search_type: str = "name"
        max_results: int = Field(default=10, ge=1, le=100)

    def _build_payload(self, query: str, search_type: str = "name", max_results: int = 10) -> dict[str, Any]:
        return {"query": query, "search_type": search_type, "max_results": max_results}
