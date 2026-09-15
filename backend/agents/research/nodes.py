from __future__ import annotations

from typing import Any

from research.state import ResearchState


def triage(state: ResearchState) -> dict[str, Any]:
    """Analyze the query and select which data sources to search."""
    raise NotImplementedError


def retrieve(state: ResearchState) -> dict[str, Any]:
    """Execute searches across selected data sources in parallel."""
    raise NotImplementedError


def synthesize(state: ResearchState) -> dict[str, Any]:
    """Combine retrieved results into a coherent answer with citations."""
    raise NotImplementedError


def cite(state: ResearchState) -> dict[str, Any]:
    """Validate all citations and emit A2UI components for rendering."""
    raise NotImplementedError
