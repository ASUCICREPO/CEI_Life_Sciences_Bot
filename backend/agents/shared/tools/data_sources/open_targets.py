from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from shared.schemas.citation import SourceType
from shared.tools.base import DataSourceTool


class OpenTargetsTool(DataSourceTool):
    function_name = "cei-data-source-open-targets"
    source_type = SourceType.OPEN_TARGETS
    description = "Search Open Targets for drug target-disease association evidence."

    class InputSchema(BaseModel):
        target: str
        disease: str = ""
        max_results: int = Field(default=10, ge=1, le=100)

    def _build_payload(self, target: str, disease: str = "", max_results: int = 10) -> dict[str, Any]:
        return {"target": target, "disease": disease, "max_results": max_results}
