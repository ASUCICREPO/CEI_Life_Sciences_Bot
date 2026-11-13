from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel, Field

from shared.schemas.citation import Citation, SourceType
from shared.tools.base import DataSourceResult, DataSourceTool
from shared.tools import retrying_http as http

logger = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "CEI-LifeSciences-Bot/0.1 (research-tool)"}
_TIMEOUT = 10
_PUG = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"


class PubChemTool(DataSourceTool):
    function_name = "cei-data-source-pubchem"
    source_type = SourceType.PUBCHEM
    description = "Search PubChem compound records for identifiers, chemical names, formulas, weights, and structure identifiers."

    class InputSchema(BaseModel):
        query: str
        search_type: str = "name"
        max_results: int = Field(default=10, ge=1, le=100)

    def _build_payload(self, query: str, search_type: str = "name", max_results: int = 10) -> dict[str, Any]:
        return {"query": query, "search_type": search_type, "max_results": max_results}

    def fetch(self, query: str, search_type: str = "name", max_results: int = 10, **_: Any) -> DataSourceResult:
        retrieved_at = datetime.now(timezone.utc)
        try:
            # Step 1: get CIDs
            cids_resp = http.get(
                f"{_PUG}/compound/name/{quote(query)}/cids/JSON",
                headers=_HEADERS,
                timeout=_TIMEOUT,
            )
            if cids_resp.status_code == 404:
                return DataSourceResult(
                    source_type=self.source_type,
                    results=[],
                    citations=[],
                    query_metadata={"query": query, "total": 0, "note": "No compound found for query."},
                )
            cids_resp.raise_for_status()
            cids: list[int] = cids_resp.json().get("IdentifierList", {}).get("CID", [])[:max_results]

            if not cids:
                return DataSourceResult(source_type=self.source_type, results=[], citations=[], query_metadata={"query": query, "total": 0})

            # Step 2: fetch properties for all CIDs
            cid_str = ",".join(str(c) for c in cids)
            props_resp = http.get(
                f"{_PUG}/compound/cid/{cid_str}/property/IUPACName,MolecularFormula,MolecularWeight,IsomericSMILES,InChIKey/JSON",
                headers=_HEADERS,
                timeout=_TIMEOUT,
            )
            props_resp.raise_for_status()
            props_data = props_resp.json().get("PropertyTable", {}).get("Properties", [])

            results: list[dict[str, Any]] = []
            citations: list[Citation] = []

            for prop in props_data:
                cid = prop.get("CID", 0)
                iupac = prop.get("IUPACName", "")
                formula = prop.get("MolecularFormula", "")
                weight = prop.get("MolecularWeight", "")
                smiles = prop.get("IsomericSMILES", "")
                inchi_key = prop.get("InChIKey", "")
                display_name = iupac or f"CID {cid}"

                result_dict = {
                    "cid": cid,
                    "iupac_name": iupac,
                    "molecular_formula": formula,
                    "molecular_weight": weight,
                    "smiles": smiles,
                    "inchi_key": inchi_key,
                }
                results.append(result_dict)
                citations.append(Citation(
                    source_id=f"CID:{cid}",
                    source_type=self.source_type,
                    url=f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}",
                    title=display_name,
                    authors=["PubChem / NCBI"],
                    year=0,
                    relevant_passage=f"Formula: {formula}. MW: {weight}. SMILES: {smiles[:80]}",
                    retrieved_at=retrieved_at,
                ))

            return DataSourceResult(
                source_type=self.source_type,
                results=results,
                citations=citations,
                query_metadata={"query": query, "total": len(results)},
            )
        except Exception as exc:
            logger.error({"event": "pubchem_fetch_error", "error_type": type(exc).__name__})
            return DataSourceResult(source_type=self.source_type, results=[], citations=[], query_metadata={"error": str(exc)})
