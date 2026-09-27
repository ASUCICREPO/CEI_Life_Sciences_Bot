from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import requests
from pydantic import BaseModel, Field

from shared.schemas.citation import Citation, SourceType
from shared.tools.base import DataSourceResult, DataSourceTool

logger = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "CEI-LifeSciences-Bot/0.1 (research-tool)"}
_TIMEOUT = 10
_API = "https://www.ebi.ac.uk/chembl/api/data"


class ChEMBLTool(DataSourceTool):
    function_name = "cei-data-source-chembl"
    source_type = SourceType.CHEMBL
    description = "Search ChEMBL molecule records for names, molecule types, synonyms, and maximum clinical phase."

    class InputSchema(BaseModel):
        query: str
        target_type: str = ""
        max_results: int = Field(default=10, ge=1, le=100)

    def _build_payload(self, query: str, target_type: str = "", max_results: int = 10) -> dict[str, Any]:
        return {"query": query, "target_type": target_type, "max_results": max_results}

    def _direct_invoke(self, query: str, target_type: str = "", max_results: int = 10, **_: Any) -> DataSourceResult:
        retrieved_at = datetime.now(timezone.utc)
        try:
            resp = requests.get(
                f"{_API}/molecule/search",
                params={"q": query, "limit": max_results, "format": "json"},
                headers=_HEADERS,
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()

            results: list[dict[str, Any]] = []
            citations: list[Citation] = []

            for mol in data.get("molecules", []):
                chembl_id = mol.get("molecule_chembl_id", "")
                pref_name = mol.get("pref_name") or chembl_id
                mol_type = mol.get("molecule_type", "")
                max_phase = mol.get("max_phase", 0)
                synonyms = [s.get("molecule_synonym", "") for s in mol.get("molecule_synonyms", [])]

                result_dict = {
                    "chembl_id": chembl_id,
                    "name": pref_name,
                    "type": mol_type,
                    "max_phase": max_phase,
                    "synonyms": synonyms,
                }
                results.append(result_dict)
                citations.append(Citation(
                    source_id=f"CHEMBL:{chembl_id}",
                    source_type=self.source_type,
                    url=f"https://www.ebi.ac.uk/chembl/compound_report_card/{chembl_id}/",
                    title=pref_name,
                    authors=["ChEMBL Database"],
                    year=0,
                    relevant_passage=f"Molecule type: {mol_type}. Max clinical phase: {max_phase}. Synonyms: {', '.join(synonyms[:5])}",
                    retrieved_at=retrieved_at,
                ))

            return DataSourceResult(
                source_type=self.source_type,
                results=results,
                citations=citations,
                query_metadata={"query": query, "total": len(results)},
            )
        except Exception as exc:
            logger.error({"event": "chembl_direct_invoke_error", "error": str(exc)})
            return DataSourceResult(source_type=self.source_type, results=[], citations=[], query_metadata={"error": str(exc)})
