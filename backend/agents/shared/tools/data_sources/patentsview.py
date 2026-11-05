from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from shared.schemas.citation import Citation, SourceType
from shared.tools.base import DataSourceResult, DataSourceTool
from shared.tools import retrying_http as http

logger = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "CEI-LifeSciences-Bot/0.1 (research-tool)"}
_TIMEOUT = 10
_API = "https://search.patentsview.org/api/v1/patent/"


class PatentsViewTool(DataSourceTool):
    function_name = "cei-data-source-patentsview"
    source_type = SourceType.PATENTSVIEW
    description = "Search USPTO PatentsView for patents by assignee, keyword, and CPC classification."

    class InputSchema(BaseModel):
        query: str
        max_results: int = Field(default=10, ge=1, le=100)

    def _build_payload(self, query: str, max_results: int = 10) -> dict[str, Any]:
        return {"query": query, "max_results": max_results}

    def fetch(self, query: str, max_results: int = 10, **_: Any) -> DataSourceResult:
        retrieved_at = datetime.now(timezone.utc)
        api_key = os.environ.get("CEI_PATENTSVIEW_API_KEY", "").strip()
        if not api_key:
            # ADR: never raise on a missing key | B97, MVP direct-mode PatentsView is optional
            logger.warning({"event": "patentsview_missing_api_key"})
            return DataSourceResult(
                source_type=self.source_type,
                results=[],
                citations=[],
                query_metadata={
                    "query": query,
                    "total": 0,
                    "note": "CEI_PATENTSVIEW_API_KEY is not set; PatentsView search was skipped.",
                },
            )

        try:
            search_query = {
                "_text_any": {"patent_title": query, "patent_abstract": query},
            }
            fields = [
                "patent_id",
                "patent_title",
                "patent_date",
                "patent_abstract",
                "assignees.assignee_organization",
            ]
            resp = http.get(
                _API,
                params={
                    "q": json.dumps(search_query),
                    "f": json.dumps(fields),
                    "o": json.dumps({"size": max_results}),
                },
                headers={**_HEADERS, "X-Api-Key": api_key},
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            payload = resp.json()

            results: list[dict[str, Any]] = []
            citations: list[Citation] = []

            for patent in payload.get("patents", [])[:max_results]:
                patent_id = str(patent.get("patent_id", ""))
                title = patent.get("patent_title", "")
                abstract = patent.get("patent_abstract", "") or ""
                date_text = patent.get("patent_date", "") or ""
                try:
                    year = int(str(date_text)[:4]) if date_text else 0
                except ValueError:
                    year = 0
                assignees = [
                    a.get("assignee_organization", "")
                    for a in (patent.get("assignees") or [])
                    if a.get("assignee_organization")
                ]

                result_dict = {
                    "patent_id": patent_id,
                    "title": title,
                    "abstract": abstract,
                    "patent_date": date_text,
                    "assignees": assignees,
                }
                results.append(result_dict)
                citations.append(Citation(
                    source_id=f"PATENT:{patent_id}",
                    source_type=self.source_type,
                    url=f"https://patents.google.com/patent/US{patent_id}",
                    title=str(title) or f"US Patent {patent_id}",
                    authors=assignees or ["Unknown assignee"],
                    year=year,
                    relevant_passage=abstract[:500] if abstract else "",
                    retrieved_at=retrieved_at,
                ))

            return DataSourceResult(
                source_type=self.source_type,
                results=results,
                citations=citations,
                query_metadata={"query": query, "total": len(results)},
            )
        except Exception as exc:
            logger.error({"event": "patentsview_fetch_error", "error": str(exc)})
            return DataSourceResult(source_type=self.source_type, results=[], citations=[], query_metadata={"error": str(exc)})
