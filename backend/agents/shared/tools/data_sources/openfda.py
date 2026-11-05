from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from shared.schemas.citation import Citation, SourceType
from shared.tools.base import DataSourceResult, DataSourceTool
from shared.tools import retrying_http as http

logger = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "CEI-LifeSciences-Bot/0.1 (research-tool)"}
_TIMEOUT = 10
_BASE = "https://api.fda.gov"

# ADR: query 3 endpoints per invoke | one openFDA source covers drug approvals,
# labels, and device clearances (B97) without a per-source endpoint parameter.
# Each template is applied per search term; openFDA reads spaces between clauses as OR.
_ENDPOINTS: tuple[tuple[str, str, str], ...] = (
    (
        "drug/drugsfda.json",
        'openfda.brand_name:"{q}" openfda.generic_name:"{q}" sponsor_name:"{q}"',
        "drug_approval",
    ),
    (
        "drug/label.json",
        'openfda.brand_name:"{q}" openfda.generic_name:"{q}" indications_and_usage:"{q}"',
        "drug_label",
    ),
    ("device/510k.json", 'device_name:"{q}" applicant:"{q}"', "device_510k"),
)
_MAX_TERMS = 6
_MAX_SEARCH_CHARS = 1_500
_INDICATION_CHARS = 600


def _search_terms(query: str) -> list[str]:
    """Split "a OR b, c" into exact-match terms (openFDA has no free-text relevance search)."""
    parts = re.split(r"\s+OR\s+|[,;|]", query, flags=re.IGNORECASE)
    terms: list[str] = []
    for part in parts:
        term = re.sub(r'["()\[\]{}:\\]', " ", part)
        term = " ".join(term.split())
        if term and term.lower() not in {t.lower() for t in terms}:
            terms.append(term)
    return terms[:_MAX_TERMS]


def _search_expression(template: str, terms: list[str]) -> str:
    return " ".join(template.format(q=term) for term in terms)[:_MAX_SEARCH_CHARS]


def _first(value: Any) -> str:
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value or "")


def _citation_url(kind: str, item: dict[str, Any], application_number: str) -> str:
    if kind == "drug_label" and item.get("set_id"):
        return f"https://dailymed.nlm.nih.gov/dailymed/lookup.cfm?setid={item['set_id']}"
    if kind == "device_510k" and application_number:
        return f"https://www.accessdata.fda.gov/scripts/cdrh/cfdocs/cfpmn/pmn.cfm?ID={application_number}"
    digits = re.sub(r"\D", "", application_number)
    if kind == "drug_approval" and digits:
        return f"https://www.accessdata.fda.gov/scripts/cder/daf/index.cfm?event=overview.process&ApplNo={digits}"
    return "https://open.fda.gov/"


class OpenFDATool(DataSourceTool):
    function_name = "cei-data-source-openfda"
    source_type = SourceType.OPENFDA
    description = (
        "Search openFDA for drug approvals (drugsfda), drug labels, and device 510(k) clearances."
    )

    class InputSchema(BaseModel):
        query: str
        max_results: int = Field(default=10, ge=1, le=100)

    def _build_payload(self, query: str, max_results: int = 10) -> dict[str, Any]:
        return {"query": query, "max_results": max_results}

    def _api_key_params(self) -> dict[str, str]:
        api_key = os.environ.get("CEI_OPENFDA_API_KEY", "").strip()
        return {"api_key": api_key} if api_key else {}

    def fetch(self, query: str, max_results: int = 10, **_: Any) -> DataSourceResult:
        retrieved_at = datetime.now(timezone.utc)
        per_endpoint_limit = max(1, max_results // len(_ENDPOINTS))
        results: list[dict[str, Any]] = []
        citations: list[Citation] = []
        errors: list[str] = []
        terms = _search_terms(query)
        if not terms:
            return DataSourceResult(
                source_type=self.source_type,
                results=[],
                citations=[],
                query_metadata={"query": query, "total": 0, "errors": ["empty query"]},
            )

        for path, search_template, kind in _ENDPOINTS:
            try:
                resp = http.get(
                    f"{_BASE}/{path}",
                    params={
                        "search": _search_expression(search_template, terms),
                        "limit": per_endpoint_limit,
                        **self._api_key_params(),
                    },
                    headers=_HEADERS,
                    timeout=_TIMEOUT,
                )
                if resp.status_code == 404:
                    # openFDA returns 404 for "no matches found" instead of an empty list.
                    continue
                resp.raise_for_status()
                for item in resp.json().get("results", [])[:per_endpoint_limit]:
                    openfda = (
                        item.get("openfda", {}) if isinstance(item.get("openfda"), dict) else {}
                    )
                    brand_names = openfda.get("brand_name") or []
                    generic_names = openfda.get("generic_name") or []
                    title = (
                        item.get("device_name")
                        or (brand_names[0] if brand_names else None)
                        or (generic_names[0] if generic_names else None)
                        or item.get("sponsor_name")
                        or item.get("applicant")
                        or "openFDA record"
                    )
                    application_number = (
                        item.get("application_number")
                        or item.get("k_number")
                        or _first(openfda.get("application_number"))
                        or ""
                    )
                    submissions = (
                        item.get("submissions") if isinstance(item.get("submissions"), list) else []
                    )
                    original = next(
                        (sub for sub in submissions if sub.get("submission_type") == "ORIG"),
                        submissions[0] if submissions else {},
                    )
                    year_source = (
                        original.get("submission_status_date")
                        or item.get("decision_date")
                        or item.get("effective_time")
                        or ""
                    )
                    try:
                        year = int(str(year_source)[:4]) if year_source else 0
                    except ValueError:
                        year = 0

                    result_dict = {
                        "kind": kind,
                        "title": title,
                        "application_number": application_number,
                        "sponsor": item.get("sponsor_name")
                        or item.get("applicant")
                        or _first(openfda.get("manufacturer_name")),
                        "date": str(year_source),
                    }
                    indications = _first(item.get("indications_and_usage"))[:_INDICATION_CHARS]
                    if indications:
                        result_dict["indications"] = indications
                    results.append(result_dict)
                    source_id = f"OPENFDA:{kind}:{application_number or title[:30]}"
                    citations.append(
                        Citation(
                            source_id=source_id,
                            source_type=self.source_type,
                            url=_citation_url(kind, item, application_number),
                            title=str(title),
                            authors=[
                                str(item.get("sponsor_name") or item.get("applicant") or "FDA")
                            ],
                            year=year,
                            relevant_passage=(
                                f"{kind} | application={application_number} | date={year_source}"
                                + (f" | indication: {indications}" if indications else "")
                            ),
                            retrieved_at=retrieved_at,
                        )
                    )
            except Exception as exc:
                # requests puts the full URL (query string, so the api_key) in HTTPError messages: never leak it
                key = self._api_key_params().get("api_key")
                error = str(exc).replace(key, "[REDACTED]") if key else str(exc)
                logger.error({"event": "openfda_fetch_error", "endpoint": path, "error": error})
                errors.append(f"{kind}: {error}")

        return DataSourceResult(
            source_type=self.source_type,
            results=results[:max_results],
            citations=citations[:max_results],
            query_metadata={
                "query": query,
                "terms": terms,
                "total": len(results),
                "errors": errors,
            },
        )
