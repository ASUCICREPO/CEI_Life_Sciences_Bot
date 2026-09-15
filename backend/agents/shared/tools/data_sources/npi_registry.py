from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from shared.schemas.citation import SourceType
from shared.tools.base import DataSourceTool


class NPIRegistryTool(DataSourceTool):
    function_name = "cei-data-source-npi-registry"
    source_type = SourceType.NPI_REGISTRY
    description = "Search the NPI Registry for healthcare provider identifiers."

    class InputSchema(BaseModel):
        name: str = ""
        npi: str = ""
        state: str = ""
        max_results: int = Field(default=10, ge=1, le=100)

    def _build_payload(self, name: str = "", npi: str = "", state: str = "", max_results: int = 10) -> dict[str, Any]:
        return {"name": name, "npi": npi, "state": state, "max_results": max_results}
