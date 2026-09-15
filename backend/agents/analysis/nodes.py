from __future__ import annotations

from typing import Any

from analysis.state import AnalysisState


def gather(state: AnalysisState) -> dict[str, Any]:
    """Collect raw data from relevant sources for the analysis type."""
    raise NotImplementedError


def analyze(state: AnalysisState) -> dict[str, Any]:
    """Process gathered data into structured analytical findings."""
    raise NotImplementedError


def compare(state: AnalysisState) -> dict[str, Any]:
    """Generate comparative matrices (SWOT, competitive, research frameworks)."""
    raise NotImplementedError


def report(state: AnalysisState) -> dict[str, Any]:
    """Produce the final analysis report with citations and A2UI components."""
    raise NotImplementedError
