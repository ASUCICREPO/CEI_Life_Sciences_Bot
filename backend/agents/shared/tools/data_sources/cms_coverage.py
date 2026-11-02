from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from shared.schemas.citation import Citation, SourceType
from shared.tools.base import DataSourceResult, DataSourceTool
from shared.tools import retrying_http as http

logger = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "CEI-LifeSciences-Bot/0.1 (research-tool)"}
_TIMEOUT = 10
# ADR: Use CMS MCD Open API for coverage search | CMS MCD website uses session-based HTML scraping; the open data API at data.cms.gov provides structured JSON
_CMS_API = "https://data.cms.gov/data-api/v1/dataset/9767cb68-8ea9-4f0b-8179-9431abc89f11/data"


class CMSCoverageTool(DataSourceTool):
    function_name = "cei-data-source-cms-coverage"
    source_type = SourceType.CMS_COVERAGE
    description = "Search CMS National Coverage Determination records for policy titles, decision memos, and effective dates."

    class InputSchema(BaseModel):
        query: str
        max_results: int = Field(default=10, ge=1, le=100)

    def _build_payload(self, query: str, max_results: int = 10) -> dict[str, Any]:
        return {"query": query, "max_results": max_results}

    def fetch(self, query: str, max_results: int = 10, **_: Any) -> DataSourceResult:
        retrieved_at = datetime.now(timezone.utc)
        try:
            resp = http.get(
                _CMS_API,
                params={"keyword": query, "size": max_results},
                headers=_HEADERS,
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()

            results: list[dict[str, Any]] = []
            citations: list[Citation] = []

            items = data if isinstance(data, list) else data.get("data", [])
            for item in items[:max_results]:
                ncd_id = str(item.get("NCD_ID", item.get("ncd_id", "")))
                title = item.get("NCD_Title", item.get("ncd_title", item.get("Title", "")))
                decision_memo = item.get("Decision_Memo", item.get("decision_memo", ""))
                effective_date = item.get("Effective_Date", item.get("effective_date", ""))
                try:
                    year = int(str(effective_date)[:4]) if effective_date else 0
                except ValueError:
                    year = 0

                result_dict = {"ncd_id": ncd_id, "title": title, "decision_memo": decision_memo, "effective_date": effective_date}
                results.append(result_dict)
                if ncd_id or title:
                    citations.append(Citation(
                        source_id=f"CMS:NCD:{ncd_id}" if ncd_id else f"CMS:{title[:30]}",
                        source_type=self.source_type,
                        url=f"https://www.cms.gov/medicare-coverage-database/view/ncd.aspx?NCDId={ncd_id}" if ncd_id else "https://www.cms.gov/medicare-coverage-database",
                        title=title or "CMS Coverage Decision",
                        authors=["Centers for Medicare & Medicaid Services"],
                        year=year,
                        relevant_passage=decision_memo[:500] if decision_memo else f"Effective date: {effective_date}",
                        retrieved_at=retrieved_at,
                    ))

            return DataSourceResult(
                source_type=self.source_type,
                results=results,
                citations=citations,
                query_metadata={
                    "query": query,
                    "total": len(results),
                    "note": "Direct mode uses CMS open data API; coverage may differ from full MCD search.",
                },
            )
        except Exception as exc:
            logger.error({"event": "cms_coverage_fetch_error", "error": str(exc)})
            return DataSourceResult(
                source_type=self.source_type,
                results=[],
                citations=[],
                query_metadata={
                    "error": str(exc),
                    "note": "CMS open data API unavailable; full coverage search requires production Lambda.",
                },
            )
