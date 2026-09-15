from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from shared.schemas.citation import SourceType
from shared.tools.base import DataSourceTool


class ChEMBLTool(DataSourceTool):
    function_name = "cei-data-source-chembl"
    source_type = SourceType.CHEMBL
    description = "Search ChEMBL for bioactive molecules and their biological targets."

    class InputSchema(BaseModel):
        query: str
        target_type: str = ""
        max_results: int = Field(default=10, ge=1, le=100)

    def _build_payload(self, query: str, target_type: str = "", max_results: int = 10) -> dict[str, Any]:
        return {"query": query, "target_type": target_type, "max_results": max_results}
