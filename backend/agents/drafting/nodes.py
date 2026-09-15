from __future__ import annotations

from typing import Any

from drafting.state import DraftingState


def research_guidelines(state: DraftingState) -> dict[str, Any]:
    """Look up latest guidelines and requirements for the target document type."""
    raise NotImplementedError


def draft(state: DraftingState) -> dict[str, Any]:
    """Generate the initial document draft using gathered guidelines and sources."""
    raise NotImplementedError


def revise(state: DraftingState) -> dict[str, Any]:
    """Incorporate user feedback and refine the draft."""
    raise NotImplementedError


def should_revise(state: DraftingState) -> str:
    """Route to revision if the user provided feedback, otherwise finalize."""
    if state.get("revision_feedback"):
        return "revise"
    return "end"
