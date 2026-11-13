from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from shared.schemas.citation import SourceType
from shared.schemas.response_blocks import ResponseDocument
from shared.state import BaseAgentState, TraceEntry
from shared.tools.base import DataSourceResult


class TriagePlan(BaseModel):
    model_config = ConfigDict(frozen=True)
    selected_sources: list[SourceType]
    source_queries: dict[str, str] = {}
    # B72: triage asks the user to clarify instead of silently falling back to PubMed.
    ambiguous: bool = False
    reasoning: str


class ResearchState(BaseAgentState):
    # Loaded using the authenticated actor; never supplied by the model.
    long_term_context: list[str]
    stream: bool
    grounding_pending: bool
    query: str
    triage_plan: TriagePlan | None
    retrieved_results: list[DataSourceResult]
    synthesis: str
    response_document: ResponseDocument | None
    # B72 clarify loop: the user's answer, fed back into the next `triage` pass; `clarified`
    # stops the loop after one round and is forced True for in-process subagent calls (drafting)
    # which have no interactive user and must keep today's silent PubMed fallback.
    clarification_answer: str | None
    clarified: bool
