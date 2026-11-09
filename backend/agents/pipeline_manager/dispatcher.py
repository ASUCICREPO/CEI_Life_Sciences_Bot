"""Dispatches one pipeline step to its target specialist agent.

MVP note (B95): dispatch is in-process (``InProcessDispatcher``), behind this
``Dispatcher`` protocol so the full-phase target — ``InvokeAgentRuntime``
per step (B83) — can replace it without touching ``pipeline_manager/nodes.py``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from langchain_core.messages import HumanMessage

from pipeline_manager.state import PipelineStep
from shared.response_text import final_ai_text
from shared.schemas.response_blocks import ResponseDocument
from shared.state import TraceEntry
from shared.streaming.trace_redaction import redacts_trace_output
from shared.tools.base import DataSourceResult

logger = logging.getLogger(__name__)

_DEPENDENCY_CONTEXT_HEADER = "Context from earlier pipeline steps (use it, do not repeat the research):"


@dataclass
class StepOutcome:
    status: str  # "completed" | "failed"
    text: str = ""
    document: ResponseDocument | None = None
    trace: list[TraceEntry] = field(default_factory=list)
    error: str | None = None
    retrieved_results: list[DataSourceResult] = field(default_factory=list)


class Dispatcher(Protocol):
    async def run(
        self,
        step: PipelineStep,
        *,
        session_id: str,
        mode: str,
        user_id: str,
        dependency_outputs: dict[str, str],
    ) -> StepOutcome: ...


def _build_prompt(step: PipelineStep, dependency_outputs: dict[str, str]) -> str:
    if not dependency_outputs:
        return step.description
    parts = [step.description, "", _DEPENDENCY_CONTEXT_HEADER]
    for dep_id, summary in dependency_outputs.items():
        parts.append(f"### Output of {dep_id}\n{summary}")
    return "\n\n".join(parts)


@redacts_trace_output  # B7: step traces are redacted at the source
def _prefixed_trace(step_id: str, entries: Any) -> list[TraceEntry]:
    prefixed: list[TraceEntry] = []
    for entry in entries or []:
        stage = entry.stage if hasattr(entry, "stage") else entry.get("stage", "")
        detail = entry.detail if hasattr(entry, "detail") else entry.get("detail", "")
        in_tok = entry.input_tokens if hasattr(entry, "input_tokens") else entry.get("input_tokens", 0)
        out_tok = entry.output_tokens if hasattr(entry, "output_tokens") else entry.get("output_tokens", 0)
        prefixed.append(
            TraceEntry(stage=f"STEP#{step_id}>{stage}", detail=detail, input_tokens=in_tok, output_tokens=out_tok)
        )
    return prefixed


_VALID_DOCUMENT_TYPES = {"grant", "vc_pitch", "journal", "whitepaper", "latex"}
_VALID_ANALYSIS_TYPES = {"competitor", "market", "legal_landscape"}


class InProcessDispatcher:
    """Runs each step's specialist graph in-process, with no checkpointer (B95).

    Mirrors ``drafting.nodes.research_subagent``: every sub-run is stateless and
    scoped to this pipeline step, so concurrent steps do not share graph state.
    """

    async def run(
        self,
        step: PipelineStep,
        *,
        session_id: str,
        mode: str,
        user_id: str,
        dependency_outputs: dict[str, str],
    ) -> StepOutcome:
        prompt = _build_prompt(step, dependency_outputs)
        sub_session_id = f"{session_id}:step:{step.step_id}"
        try:
            if step.agent == "research":
                return await self._run_research(prompt, mode, user_id, sub_session_id, step.step_id)
            if step.agent == "drafting":
                return await self._run_drafting(prompt, mode, user_id, sub_session_id, step.step_id, step.document_type)
            if step.agent == "analysis":
                return await self._run_analysis(prompt, mode, sub_session_id, step.step_id, step.analysis_type, step.scope)
            return StepOutcome(status="failed", error=f"unknown agent {step.agent!r}")
        except Exception as exc:  # ADR: catch-all | one bad step must not crash the pipeline run
            logger.error({"event": "dispatch_step_error", "step_id": step.step_id, "agent": step.agent, "error_type": type(exc).__name__})
            return StepOutcome(status="failed", error=repr(exc))

    async def _run_research(
        self, prompt: str, mode: str, user_id: str, session_id: str, step_id: str
    ) -> StepOutcome:
        from research.graph import compile_graph as compile_research_graph

        graph = compile_research_graph()
        initial_state = {
            "messages": [HumanMessage(content=prompt)],
            "mode": mode,
            "user_id": user_id,
            "session_id": session_id,
            "query": prompt,
            "triage_plan": None,
            "retrieved_results": [],
            "synthesis": "",
            "response_document": None,
            "trace": [],
        }
        result = await graph.ainvoke(initial_state, config={"configurable": {"thread_id": session_id}})
        text = final_ai_text(result.get("messages", [])) or ""
        return StepOutcome(
            status="completed",
            text=text,
            document=result.get("response_document"),
            retrieved_results=result.get("retrieved_results", []),
            trace=_prefixed_trace(step_id, result.get("trace", [])),
        )

    async def _run_drafting(
        self, prompt: str, mode: str, user_id: str, session_id: str, step_id: str, document_type: str | None
    ) -> StepOutcome:
        from drafting.graph import compile_graph as compile_drafting_graph
        from drafting.state import DocumentType

        doc_type_raw = document_type if document_type in _VALID_DOCUMENT_TYPES else "whitepaper"
        graph = compile_drafting_graph()
        initial_state = {
            "messages": [HumanMessage(content=prompt)],
            "mode": mode,
            "user_id": user_id,
            "session_id": session_id,
            "document_type": DocumentType(doc_type_raw),
            "guidelines": [],
            "research_questions": [],
            "research_findings": [],
            "draft": "",
            "draft_document": None,
            "revision_feedback": None,
            "trace": [],
            "export_formats": ["md", "txt"],
            "chart_specs": [],
            "chart_data": {},
            "chart_instructions": None,
            "exports": [],
        }
        result = await graph.ainvoke(initial_state, config={"configurable": {"thread_id": session_id}})
        text = final_ai_text(result.get("messages", [])) or ""
        return StepOutcome(
            status="completed",
            text=text,
            document=result.get("draft_document"),
            trace=_prefixed_trace(step_id, result.get("trace", [])),
        )

    async def _run_analysis(
        self,
        prompt: str,
        mode: str,
        session_id: str,
        step_id: str,
        analysis_type: str | None,
        scope: dict[str, Any] | None,
    ) -> StepOutcome:
        from analysis.run import run_analysis

        resolved_type = analysis_type if analysis_type in _VALID_ANALYSIS_TYPES else None
        outcome = await run_analysis(prompt, mode, session_id, analysis_type=resolved_type, scope=scope)
        return StepOutcome(
            status="completed",
            text=outcome.text,
            document=outcome.document,
            retrieved_results=outcome.retrieved_results,
            trace=_prefixed_trace(step_id, outcome.trace),
        )
