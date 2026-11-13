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
# ADR: Use Europe PMC for medRxiv search | same reasoning as bioRxiv; PUBLISHER:medRxiv filter narrows to health-science preprints
_EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"


class MedRxivTool(DataSourceTool):
    function_name = "cei-data-source-medrxiv"
    source_type = SourceType.MEDRXIV
    description = "Search medRxiv health-science preprints for titles, abstracts, authors, years, and DOIs."

    class InputSchema(BaseModel):
        query: str
        max_results: int = Field(default=10, ge=1, le=100)

    def _build_payload(self, query: str, max_results: int = 10) -> dict[str, Any]:
        return {"query": query, "max_results": max_results}

    def fetch(self, query: str, max_results: int = 10, **_: Any) -> DataSourceResult:
        retrieved_at = datetime.now(timezone.utc)
        try:
            resp = http.get(
                _EPMC,
                params={
                    "query": f"{query} SRC:PPR PUBLISHER:medRxiv",
                    "format": "json",
                    "pageSize": max_results,
                    "resultType": "core",
                },
                headers=_HEADERS,
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()

            results: list[dict[str, Any]] = []
            citations: list[Citation] = []

            for item in data.get("resultList", {}).get("result", []):
                doi = item.get("doi", "")
                title = item.get("title", "")
                abstract = item.get("abstractText", "")
                pub_year_str = item.get("pubYear", "0")
                try:
                    year = int(pub_year_str)
                except ValueError:
                    year = 0
                authors_str = item.get("authorString", "")
                authors = [a.strip() for a in authors_str.split(",") if a.strip()]

                result_dict = {"doi": doi, "title": title, "abstract": abstract, "year": year, "authors": authors}
                results.append(result_dict)
                citations.append(Citation(
                    source_id=f"doi:{doi}" if doi else f"epmc:{item.get('id', '')}",
                    source_type=self.source_type,
                    url=f"https://doi.org/{doi}" if doi else f"https://europepmc.org/article/{item.get('source', '')}/{item.get('id', '')}",
                    title=title,
                    authors=authors,
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
            logger.error({"event": "medrxiv_fetch_error", "error_type": type(exc).__name__})
            return DataSourceResult(source_type=self.source_type, results=[], citations=[], query_metadata={"error": str(exc)})
