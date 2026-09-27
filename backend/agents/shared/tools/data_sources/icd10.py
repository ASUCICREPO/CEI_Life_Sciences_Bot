from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import requests
from pydantic import BaseModel, Field

from shared.schemas.citation import Citation, SourceType
from shared.tools.base import DataSourceResult, DataSourceTool

logger = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "CEI-LifeSciences-Bot/0.1 (research-tool)"}
_TIMEOUT = 10
_API = "https://clinicaltables.nlm.nih.gov/api/icd10cm/v3/search"


class ICD10Tool(DataSourceTool):
    function_name = "cei-data-source-icd10"
    source_type = SourceType.ICD10
    description = "Search ICD-10-CM diagnosis classification records for codes and condition names."

    class InputSchema(BaseModel):
        query: str
        max_results: int = Field(default=10, ge=1, le=100)

    def _build_payload(self, query: str, max_results: int = 10) -> dict[str, Any]:
        return {"query": query, "max_results": max_results}

    def _direct_invoke(self, query: str, max_results: int = 10, **_: Any) -> DataSourceResult:
        retrieved_at = datetime.now(timezone.utc)
        try:
            resp = requests.get(
                _API,
                params={"sf": "code,name", "terms": query, "maxList": max_results},
                headers=_HEADERS,
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            # Response: [total_count, codes_list, null, [[code, name], ...]]
            payload = resp.json()
            code_name_pairs = payload[3] if len(payload) > 3 and payload[3] else []

            results: list[dict[str, Any]] = []
            citations: list[Citation] = []

            for pair in code_name_pairs:
                code = pair[0] if pair else ""
                name = pair[1] if len(pair) > 1 else ""

                result_dict = {"code": code, "name": name}
                results.append(result_dict)
                citations.append(Citation(
                    source_id=f"ICD10:{code}",
                    source_type=self.source_type,
                    url=f"https://icd.who.int/browse10/2019/en#/{code}",
                    title=f"{code} — {name}",
                    authors=["World Health Organization / NLM"],
                    year=2019,
                    relevant_passage=f"ICD-10-CM code {code}: {name}",
                    retrieved_at=retrieved_at,
                ))

            return DataSourceResult(
                source_type=self.source_type,
                results=results,
                citations=citations,
                query_metadata={"query": query, "total": len(results)},
            )
        except Exception as exc:
            logger.error({"event": "icd10_direct_invoke_error", "error": str(exc)})
            return DataSourceResult(source_type=self.source_type, results=[], citations=[], query_metadata={"error": str(exc)})
