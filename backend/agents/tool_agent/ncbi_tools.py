"""The read-only NCBI tools available to an NCBI-scoped session.

BLAST is deliberately absent until submitted request IDs can be bound to a
trusted user and safely resumed. No direct network fallback is permitted here.
"""
from __future__ import annotations

import os
from typing import Any, Literal

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from shared.tools.gateway_context import current_gateway_token
from shared.tools.gateway_mcp import GatewayCallError, call_gateway_tool


class TaxonomyInput(BaseModel):
    taxon: str = Field(min_length=1, max_length=120)


class SequenceInput(BaseModel):
    accession: str = Field(min_length=1, max_length=45)
    database: Literal["nuccore", "protein"]
    format: Literal["fasta", "gb"]


def _call(target: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    token = current_gateway_token()
    endpoint = os.environ.get("CEI_TOOL_GATEWAY_URL", "")
    if not token or not endpoint:
        raise GatewayCallError("The data source gateway is unavailable.")
    return call_gateway_tool(endpoint, token, target, arguments, tool_name=tool)


def _taxonomy(taxon: str) -> dict[str, Any]:
    return _call("ncbi-taxonomy", "lineage", {"taxon": taxon})


def _sequence(accession: str, database: str, format: str) -> dict[str, Any]:
    return _call("ncbi-sequence", "efetch", {"accession": accession, "database": database, "format": format})


def ncbi_tools() -> list[StructuredTool]:
    return [
        StructuredTool.from_function(_taxonomy, name="ncbi_taxonomy_lineage",
                                     description="Retrieve an NCBI taxon's lineage.", args_schema=TaxonomyInput),
        StructuredTool.from_function(_sequence, name="ncbi_sequence_efetch",
                                     description="Retrieve an NCBI nucleotide or protein sequence.", args_schema=SequenceInput),
    ]
