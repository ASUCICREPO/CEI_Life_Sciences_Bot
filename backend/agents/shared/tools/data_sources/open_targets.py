from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import requests
from pydantic import BaseModel, Field

from shared.schemas.citation import Citation, SourceType
from shared.tools.base import DataSourceResult, DataSourceTool

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": "CEI-LifeSciences-Bot/0.1 (research-tool)",
    "Content-Type": "application/json",
}
_TIMEOUT = 10
_GRAPHQL = "https://api.platform.opentargets.org/api/v4/graphql"

_SEARCH_QUERY = """
query Search($q: String!, $size: Int!) {
  search(queryString: $q, entityNames: ["target", "disease", "drug"], page: {index: 0, size: $size}) {
    total
    hits {
      id
      entity
      name
      description
    }
  }
}
"""


class OpenTargetsTool(DataSourceTool):
    function_name = "cei-data-source-open-targets"
    source_type = SourceType.OPEN_TARGETS
    description = "Search Open Targets records for target, disease, and drug names with descriptions and identifiers."

    class InputSchema(BaseModel):
        target: str
        disease: str = ""
        max_results: int = Field(default=10, ge=1, le=100)

    def _build_payload(self, target: str, disease: str = "", max_results: int = 10) -> dict[str, Any]:
        return {"target": target, "disease": disease, "max_results": max_results}

    def _direct_invoke(self, target: str = "", disease: str = "", max_results: int = 10, query: str = "", **_: Any) -> DataSourceResult:
        retrieved_at = datetime.now(timezone.utc)
        if not target and query:
            target = query
        query_str = f"{target} {disease}".strip()
        try:
            resp = requests.post(
                _GRAPHQL,
                json={"query": _SEARCH_QUERY, "variables": {"q": query_str, "size": max_results}},
                headers=_HEADERS,
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()

            results: list[dict[str, Any]] = []
            citations: list[Citation] = []

            hits = data.get("data", {}).get("search", {}).get("hits", [])
            for hit in hits:
                ot_id = hit.get("id", "")
                entity = hit.get("entity", "")
                name = hit.get("name", "")
                description = hit.get("description", "")

                if entity == "target":
                    url = f"https://platform.opentargets.org/target/{ot_id}"
                elif entity == "disease":
                    url = f"https://platform.opentargets.org/disease/{ot_id}"
                else:
                    url = f"https://platform.opentargets.org/drug/{ot_id}"

                result_dict = {"id": ot_id, "entity": entity, "name": name, "description": description}
                results.append(result_dict)
                citations.append(Citation(
                    source_id=f"OT:{ot_id}",
                    source_type=self.source_type,
                    url=url,
                    title=name,
                    authors=["Open Targets Platform"],
                    year=0,
                    relevant_passage=description[:500] if description else f"Entity type: {entity}",
                    retrieved_at=retrieved_at,
                ))

            return DataSourceResult(
                source_type=self.source_type,
                results=results,
                citations=citations,
                query_metadata={"query": query_str, "total": len(results)},
            )
        except Exception as exc:
            logger.error({"event": "open_targets_direct_invoke_error", "error": str(exc)})
            return DataSourceResult(source_type=self.source_type, results=[], citations=[], query_metadata={"error": str(exc)})
