from __future__ import annotations

import html
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from shared.schemas.citation import Citation, SourceType
from shared.tools.base import DataSourceResult, DataSourceTool
from shared.tools import retrying_http as http

logger = logging.getLogger(__name__)
_HEADERS = {"User-Agent": "CEI-LifeSciences-Bot/0.1 (research-tool)"}
_TIMEOUT = 10
_REPORT = "https://api.coverage.cms.gov/v1/reports/national-coverage-ncd/"
_DETAIL = "https://api.coverage.cms.gov/v1/data/ncd/"
# Keep one Gateway invocation bounded even when max_results is 100.
_DETAIL_LIMIT = 10


def _plain_text(value: Any) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]*>", " ", str(value or ""))).split())


def _detail(item: dict[str, Any]) -> dict[str, Any]:
    try:
        response = http.get(_DETAIL, params={"ncdid": item["document_id"], "ncdver": item["document_version"]}, headers=_HEADERS, timeout=_TIMEOUT)
        response.raise_for_status()
        payload = response.json()
        records = payload.get("data", []) if isinstance(payload, dict) else []
        return records[0] if isinstance(records, list) and records and isinstance(records[0], dict) else {}
    except Exception as exc:
        logger.warning({"event": "cms_ncd_detail_error", "error_type": type(exc).__name__})
        return {}


class CMSCoverageTool(DataSourceTool):
    function_name = "cei-data-source-cms-coverage"
    source_type = SourceType.CMS_COVERAGE
    description = "Search CMS National Coverage Determination titles and display IDs, with effective dates for up to ten results."

    class InputSchema(BaseModel):
        query: str
        max_results: int = Field(default=10, ge=1, le=100)

    def _build_payload(self, query: str, max_results: int = 10) -> dict[str, Any]:
        return {"query": query, "max_results": max_results}

    def fetch(self, query: str, max_results: int = 10, **_: Any) -> DataSourceResult:
        retrieved_at = datetime.now(timezone.utc)
        terms = re.findall(r"[\w.]+", query.casefold())
        if not terms:
            return DataSourceResult(source_type=self.source_type, results=[], citations=[], query_metadata={"query": query, "total": 0})
        try:
            response = http.get(_REPORT, headers=_HEADERS, timeout=_TIMEOUT)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise ValueError("Unexpected CMS coverage report response")
            if (payload.get("meta") or {}).get("next_token"):
                raise ValueError("CMS coverage report has unsupported pagination")
            # This report endpoint has no search parameters; filter its full list locally.
            matches = [item for item in payload["data"] if isinstance(item, dict)
                       and item.get("document_id") is not None and item.get("document_version") is not None
                       and all(term in f"{item.get('title', '')} {item.get('document_display_id', '')}".casefold() for term in terms)]
            selected = matches[:max_results]
            with ThreadPoolExecutor(max_workers=_DETAIL_LIMIT) as executor:
                details = list(executor.map(_detail, selected[:_DETAIL_LIMIT]))
            results, citations = [], []
            for i, item in enumerate(selected):
                detail = details[i] if i < len(details) else {}
                ncd_id = str(item["document_id"])
                title = str(item.get("title") or "CMS Coverage Decision")
                effective_date = str(detail.get("effective_date") or "")
                passage = _plain_text(detail.get("indications_limitations") or detail.get("item_service_description"))[:500]
                try:
                    year = datetime.strptime(effective_date[:10], "%m/%d/%Y").year if effective_date else 0
                except ValueError:
                    year = 0
                results.append({"ncd_id": ncd_id, "title": title, "decision_memo": "", "effective_date": effective_date,
                                "display_id": str(item.get("document_display_id") or ""), "last_updated": str(item.get("last_updated") or ""),
                                "detail_available": bool(detail)})
                citations.append(Citation(source_id=f"CMS:NCD:{ncd_id}", source_type=self.source_type,
                    url=f"https://www.cms.gov/medicare-coverage-database/view/ncd.aspx?NCDId={ncd_id}", title=title,
                    authors=["Centers for Medicare & Medicaid Services"], year=year,
                    relevant_passage=passage or f"National Coverage Determination {item.get('document_display_id', '')}", retrieved_at=retrieved_at))
            return DataSourceResult(source_type=self.source_type, results=results, citations=citations,
                query_metadata={"query": query, "total": len(matches), "search_scope": "NCD titles and display IDs",
                                "note": "Full details are fetched for at most 10 results; decision memos are separate NCA documents."})
        except Exception as exc:
            logger.error({"event": "cms_coverage_fetch_error", "error_type": type(exc).__name__})
            return DataSourceResult(source_type=self.source_type, results=[], citations=[],
                query_metadata={"query": query, "error": "CMS Coverage API unavailable or returned an invalid response."})
