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
_API = "https://npiregistry.cms.hhs.gov/api/"


class NPIRegistryTool(DataSourceTool):
    function_name = "cei-data-source-npi-registry"
    source_type = SourceType.NPI_REGISTRY
    description = "Search NPI Registry provider records for identifiers, names, credentials, taxonomy, and provider type."

    class InputSchema(BaseModel):
        name: str = ""
        npi: str = ""
        state: str = ""
        max_results: int = Field(default=10, ge=1, le=100)

    def _build_payload(self, name: str = "", npi: str = "", state: str = "", max_results: int = 10) -> dict[str, Any]:
        return {"name": name, "npi": npi, "state": state, "max_results": max_results}

    def fetch(self, name: str = "", npi: str = "", state: str = "", max_results: int = 10, query: str = "", **_: Any) -> DataSourceResult:
        if not name and not npi and query:
            name = query
        retrieved_at = datetime.now(timezone.utc)
        try:
            params: dict[str, Any] = {"version": "2.1", "limit": max_results}
            if npi:
                params["number"] = npi
            elif name:
                # Split name into first/last if space present, else treat as org name
                parts = name.strip().split(" ", 1)
                if len(parts) == 2:
                    params["first_name"] = parts[0]
                    params["last_name"] = parts[1]
                else:
                    params["organization_name"] = name
            if state:
                params["state"] = state

            resp = http.get(_API, params=params, headers=_HEADERS, timeout=_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()

            results: list[dict[str, Any]] = []
            citations: list[Citation] = []

            for provider in data.get("results", []):
                npi_number = provider.get("number", "")
                basic = provider.get("basic", {})
                first = basic.get("first_name", "")
                last = basic.get("last_name", "")
                org = basic.get("organization_name", "")
                provider_name = f"{first} {last}".strip() or org
                credential = basic.get("credential", "")
                taxonomy = provider.get("taxonomies", [{}])[0].get("desc", "") if provider.get("taxonomies") else ""
                enum_type = provider.get("enumeration_type", "")

                result_dict = {
                    "npi": npi_number,
                    "name": provider_name,
                    "credential": credential,
                    "taxonomy": taxonomy,
                    "type": enum_type,
                }
                results.append(result_dict)
                citations.append(Citation(
                    source_id=f"NPI:{npi_number}",
                    source_type=self.source_type,
                    url=f"https://npiregistry.cms.hhs.gov/provider/{npi_number}",
                    title=f"{provider_name} {credential}".strip(),
                    authors=["CMS NPI Registry"],
                    year=0,
                    relevant_passage=f"NPI: {npi_number}. Taxonomy: {taxonomy}. Type: {enum_type}.",
                    retrieved_at=retrieved_at,
                ))

            return DataSourceResult(
                source_type=self.source_type,
                results=results,
                citations=citations,
                query_metadata={"name": name, "npi": npi, "total": len(results)},
            )
        except Exception as exc:
            logger.error({"event": "npi_registry_fetch_error", "error_type": type(exc).__name__})
            return DataSourceResult(source_type=self.source_type, results=[], citations=[], query_metadata={"error": str(exc)})
