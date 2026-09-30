from __future__ import annotations

from typing import Any

from pipeline_manager.state import PipelineState, StepStatusValue


def plan(state: PipelineState) -> dict[str, Any]:
    """Generate a step-by-step execution plan with dependencies and time estimates."""
    raise NotImplementedError


def approve(state: PipelineState) -> dict[str, Any]:
    """Present the plan to the user via CEI UI ReviewGate and wait for approval."""
    raise NotImplementedError


def dispatch(state: PipelineState) -> dict[str, Any]:
    """Launch sub-tasks on the appropriate agents (concurrent, sequential, or looped)."""
    raise NotImplementedError


def monitor(state: PipelineState) -> dict[str, Any]:
    """Check step statuses and handle failures with retry/alternative/escalation."""
    raise NotImplementedError


def synthesize(state: PipelineState) -> dict[str, Any]:
    """Combine results from all completed steps into a unified output."""
    raise NotImplementedError


def should_continue(state: PipelineState) -> str:
    """Route based on pipeline state: monitor running steps, synthesize when done, or retry."""
    steps = state.get("steps", [])
    if not steps:
        return "dispatch"
    statuses = {s.status if hasattr(s, "status") else s["status"] for s in steps}
    if StepStatusValue.RUNNING in statuses:
        return "monitor"
    if StepStatusValue.FAILED in statuses and state.get("retry_count", 0) < 3:
        return "dispatch"
    return "synthesize"
