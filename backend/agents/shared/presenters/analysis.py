"""Deterministic presenter for the Analysis agent's final CEI UI surface."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from pydantic import ValidationError

from shared.presenters.cei_ui import (
    _build_evidence_components,
    _build_research_document_surface,
    _collect_citations,
    _build_research_legacy_surface,
)
from shared.schemas.cei_ui import CEIUISurface
from shared.schemas.response_blocks import ResponseDocument

logger = logging.getLogger(__name__)


def build_analysis_surface(
    text: str,
    *,
    surface_id: str,
    retrieved_results: Sequence[object],
    document: ResponseDocument | None = None,
) -> CEIUISurface:
    """Build the Analysis agent's final surface, reusing the research document renderer (B94/F14)."""
    citations, total_citations = _collect_citations(retrieved_results)
    evidence_components = _build_evidence_components(citations, total_citations)

    if document is not None:
        try:
            return _build_research_document_surface(surface_id, document, citations, evidence_components)
        except ValidationError:
            logger.warning({"event": "analysis_document_surface_fallback"})

    return _build_research_legacy_surface(text, surface_id, evidence_components)
