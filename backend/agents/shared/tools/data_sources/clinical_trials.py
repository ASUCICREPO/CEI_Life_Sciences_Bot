from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote_plus

from pydantic import BaseModel, Field

from shared.schemas.citation import Citation, SourceType
from shared.tools.base import DataSourceResult, DataSourceTool
from shared.tools import retrying_http as http

logger = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "CEI-LifeSciences-Bot/0.1 (research-tool)"}
_TIMEOUT = 10
_API = "https://clinicaltrials.gov/api/v2/studies"


class ClinicalTrialsTool(DataSourceTool):
    function_name = "cei-data-source-clinical-trials"
    source_type = SourceType.CLINICAL_TRIALS
    description = "Search ClinicalTrials.gov study records for NCT IDs, titles, recruitment status, summaries, and start years."

    class InputSchema(BaseModel):
        query: str
        status: str = ""
        max_results: int = Field(default=10, ge=1, le=100)

    def _build_payload(self, query: str, status: str = "", max_results: int = 10) -> dict[str, Any]:
        return {"query": query, "status": status, "max_results": max_results}

    def fetch(self, query: str, status: str = "", max_results: int = 10, **_: Any) -> DataSourceResult:
        retrieved_at = datetime.now(timezone.utc)
        try:
            params: dict[str, Any] = {"query.term": query, "pageSize": max_results}
            if status:
                params["filter.overallStatus"] = status
            resp = http.get(_API, params=params, headers=_HEADERS, timeout=_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()

            results: list[dict[str, Any]] = []
            citations: list[Citation] = []

            for study in data.get("studies", []):
                proto = study.get("protocolSection", {})
                id_mod = proto.get("identificationModule", {})
                status_mod = proto.get("statusModule", {})
                desc_mod = proto.get("descriptionModule", {})

                nct_id = id_mod.get("nctId", "")
                title = id_mod.get("briefTitle", "")
                brief_summary = desc_mod.get("briefSummary", "")
                overall_status = status_mod.get("overallStatus", "")
                start_year_str = status_mod.get("startDateStruct", {}).get("date", "")
                try:
                    year = int(start_year_str[:4]) if start_year_str else 0
                except ValueError:
                    year = 0

                result_dict = {
                    "nct_id": nct_id,
                    "title": title,
                    "status": overall_status,
                    "summary": brief_summary,
                    "year": year,
                }
                results.append(result_dict)
                citations.append(Citation(
                    source_id=f"NCT{nct_id}" if not nct_id.startswith("NCT") else nct_id,
                    source_type=self.source_type,
                    url=f"https://clinicaltrials.gov/study/{nct_id}",
                    title=title,
                    authors=[],
                    year=year,
                    relevant_passage=brief_summary[:500] if brief_summary else overall_status,
                    retrieved_at=retrieved_at,
                ))

            return DataSourceResult(
                source_type=self.source_type,
                results=results,
                citations=citations,
                query_metadata={"query": query, "total": len(results)},
            )
        except Exception as exc:
            logger.error({"event": "clinical_trials_fetch_error", "error_type": type(exc).__name__})
            return DataSourceResult(source_type=self.source_type, results=[], citations=[], query_metadata={"error": str(exc)})
