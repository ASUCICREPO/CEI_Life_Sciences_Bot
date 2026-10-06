from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

import requests
from pydantic import BaseModel, Field

from shared.schemas.citation import Citation, SourceType
from shared.tools.base import DataSourceResult, DataSourceTool

logger = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "CEI-LifeSciences-Bot/0.1 (research-tool)"}
_TIMEOUT = 10
_BASE = "https://api.fda.gov"

# ADR: query 3 endpoints per invoke | one openFDA source covers drug approvals,
# labels, and device clearances (B97) without a per-source endpoint parameter.
_ENDPOINTS: tuple[tuple[str, str, str], ...] = (
    ("drug/drugsfda.json", 'openfda.brand_name:"{q}" openfda.generic_name:"{q}" sponsor_name:"{q}"', "drug_approval"),
    ("drug/label.json", 'openfda.brand_name:"{q}" openfda.generic_name:"{q}"', "drug_label"),
    ("device/510k.json", 'device_name:"{q}" applicant:"{q}"', "device_510k"),
)


class OpenFDATool(DataSourceTool):
    function_name = "cei-data-source-openfda"
    source_type = SourceType.OPENFDA
    description = "Search openFDA for drug approvals (drugsfda), drug labels, and device 510(k) clearances."

    class InputSchema(BaseModel):
        query: str
        max_results: int = Field(default=10, ge=1, le=100)

    def _build_payload(self, query: str, max_results: int = 10) -> dict[str, Any]:
        return {"query": query, "max_results": max_results}

    def _api_key_params(self) -> dict[str, str]:
        api_key = os.environ.get("CEI_OPENFDA_API_KEY", "").strip()
        return {"api_key": api_key} if api_key else {}

    def _direct_invoke(self, query: str, max_results: int = 10, **_: Any) -> DataSourceResult:
        retrieved_at = datetime.now(timezone.utc)
        per_endpoint_limit = max(1, max_results // len(_ENDPOINTS))
        results: list[dict[str, Any]] = []
        citations: list[Citation] = []
        errors: list[str] = []

        for path, search_template, kind in _ENDPOINTS:
            try:
                resp = requests.get(
                    f"{_BASE}/{path}",
                    params={
                        "search": search_template.format(q=query),
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
                    openfda = item.get("openfda", {}) if isinstance(item.get("openfda"), dict) else {}
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
                    application_number = item.get("application_number") or item.get("k_number") or ""
                    year_source = item.get("submission_status_date") or item.get("decision_date") or ""
                    try:
                        year = int(str(year_source)[:4]) if year_source else 0
                    except ValueError:
                        year = 0

                    result_dict = {
                        "kind": kind,
                        "title": title,
                        "application_number": application_number,
                        "sponsor": item.get("sponsor_name") or item.get("applicant") or "",
                    }
                    results.append(result_dict)
                    source_id = f"OPENFDA:{kind}:{application_number or title[:30]}"
                    citations.append(Citation(
                        source_id=source_id,
                        source_type=self.source_type,
                        url=f"https://www.accessdata.fda.gov/scripts/cder/daf/" if kind != "device_510k"
                        else "https://www.accessdata.fda.gov/scripts/cdrh/cfdocs/cfpmn/pmn.cfm",
                        title=str(title),
                        authors=[str(item.get("sponsor_name") or item.get("applicant") or "FDA")],
                        year=year,
                        relevant_passage=f"{kind} | application={application_number}",
                        retrieved_at=retrieved_at,
                    ))
            except Exception as exc:
                logger.error({"event": "openfda_direct_invoke_error", "endpoint": path, "error": str(exc)})
                errors.append(f"{kind}: {exc!s}")

        return DataSourceResult(
            source_type=self.source_type,
            results=results[:max_results],
            citations=citations[:max_results],
            query_metadata={"query": query, "total": len(results), "errors": errors},
        )
