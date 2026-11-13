from __future__ import annotations

import logging
import os
import random
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any
from functools import lru_cache
from urllib.parse import quote_plus

from pydantic import BaseModel, Field

from shared.schemas.citation import Citation, SourceType
from shared.tools.base import DataSourceResult, DataSourceTool
from shared.tools import retrying_http as http

logger = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "CEI-LifeSciences-Bot/0.1 (research-tool)"}
_TIMEOUT = 10
_ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
_EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"


@lru_cache(maxsize=1)
def _shared_limiter():
    # The Lambda layer stages the same limiter implementation as the NCBI target.
    import boto3
    from ncbi_limiter import NCBIRateLimiter
    return NCBIRateLimiter(boto3.client("dynamodb"), os.environ["NCBI_RATE_LIMIT_TABLE"])


def _acquire_eutils_slot() -> None:
    from ncbi_limiter import RateLimited
    for attempt in range(10):
        try:
            _shared_limiter().acquire("eutils")
            return
        except RateLimited:
            if attempt == 9:
                raise
            time.sleep(random.uniform(0.36, 0.46))


class PubMedTool(DataSourceTool):
    function_name = "cei-data-source-pubmed"
    source_type = SourceType.PUBMED
    description = "Search PubMed biomedical article records for citations, abstracts, authors, publication years, and PMIDs."
    # B20: an NCBI API key raises the E-utilities limit from 3 to 10 requests/second
    supports_api_key = True

    class InputSchema(BaseModel):
        query: str
        max_results: int = Field(default=10, ge=1, le=100)

    def _build_payload(self, query: str, max_results: int = 10) -> dict[str, Any]:
        return {"query": query, "max_results": max_results}

    def fetch(self, query: str, max_results: int = 10, api_key: str | None = None, **_: Any) -> DataSourceResult:
        retrieved_at = datetime.now(timezone.utc)
        key_param = {"api_key": api_key} if api_key else {}
        throttle = {"before_request": _acquire_eutils_slot} if os.environ.get("NCBI_RATE_LIMIT_TABLE") else {}
        try:
            search_resp = http.get(
                _ESEARCH,
                params={"db": "pubmed", "term": query, "retmax": max_results, "retmode": "json", **key_param},
                headers=_HEADERS,
                timeout=_TIMEOUT,
                **throttle,
            )
            search_resp.raise_for_status()
            ids: list[str] = search_resp.json().get("esearchresult", {}).get("idlist", [])
            if not ids:
                return DataSourceResult(source_type=self.source_type, results=[], citations=[], query_metadata={"query": query, "total": 0})

            fetch_resp = http.get(
                _EFETCH,
                params={"db": "pubmed", "id": ",".join(ids), "rettype": "abstract", "retmode": "xml", **key_param},
                headers=_HEADERS,
                timeout=_TIMEOUT,
                **throttle,
            )
            fetch_resp.raise_for_status()
            root = ET.fromstring(fetch_resp.text)

            results: list[dict[str, Any]] = []
            citations: list[Citation] = []

            for article in root.findall(".//PubmedArticle"):
                pmid_el = article.find(".//PMID")
                pmid = pmid_el.text if pmid_el is not None else ""
                title_el = article.find(".//ArticleTitle")
                title = title_el.text or "" if title_el is not None else ""
                # Structured abstracts split into labeled parts; RESULTS holds the numbers.
                abstract = "\n".join(
                    (f"{part.get('Label')}: " if part.get("Label") else "")
                    + "".join(part.itertext()).strip()
                    for part in article.findall(".//Abstract/AbstractText")
                )
                year_el = article.find(".//PubDate/Year")
                year_text = year_el.text if year_el is not None else ""
                year = int(year_text) if year_text and year_text.isdigit() else 0
                authors = [
                    f"{a.findtext('LastName', '')} {a.findtext('ForeName', '')}".strip()
                    for a in article.findall(".//Author")
                ]

                result_dict = {"pmid": pmid, "title": title, "abstract": abstract, "year": year, "authors": authors}
                results.append(result_dict)
                citations.append(Citation(
                    source_id=f"PMID:{pmid}",
                    source_type=self.source_type,
                    url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
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
            # requests puts the full URL (query string included) in HTTPError messages: never leak the key
            error = str(exc).replace(api_key, "[REDACTED]") if api_key else str(exc)
            logger.error({"event": "pubmed_fetch_error", "error_type": type(exc).__name__})
            return DataSourceResult(source_type=self.source_type, results=[], citations=[], query_metadata={"error": error})
