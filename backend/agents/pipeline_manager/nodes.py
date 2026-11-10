from __future__ import annotations

import asyncio
import re
import logging
from pathlib import Path
from typing import Any, Mapping

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from pipeline_manager.dispatcher import InProcessDispatcher, StepOutcome
from pipeline_manager.state import (
    PipelinePlan,
    PipelineState,
    PipelineStep,
    StepStatus,
    StepStatusValue,
)
from shared.grounding import build_corpus, ground_document
from shared.presenters.markdown import render_document_markdown
from shared.prompts.loader import PromptLoader
from shared.runtime.durable_jobs import active_job_store
from shared.schemas.response_blocks import ResponseDocument
from shared.state import TraceEntry
from shared.structured import invoke_structured
from shared.tools.hitl.inline_functions import PlanStepInput, build_plan_approval_surface
from shared.utils import extract_tokens, get_model, last_human_message

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "prompts" / "templates"

_ALLOWED_AGENTS = frozenset({"research", "drafting", "analysis"})
MAX_PLAN_STEPS = 6
MAX_RETRIES = 3
_DEPENDENCY_SUMMARY_CHARS = 4_000
_SYNTHESIS_MAX_TOKENS = 8192

_dispatcher = InProcessDispatcher()


# ── plan ──────────────────────────────────────────────────────────────────


def _has_cycle(steps: list[PipelineStep]) -> bool:
    deps = {step.step_id: step.dependencies for step in steps}
    visited: set[str] = set()
    stack: set[str] = set()

    def visit(node: str) -> bool:
        if node in stack:
            return True
        if node in visited:
            return False
        stack.add(node)
        for dep in deps.get(node, []):
            if dep in deps and visit(dep):
                return True
        stack.discard(node)
        visited.add(node)
        return False

    return any(visit(step.step_id) for step in steps)


def _validate_plan(plan: PipelinePlan | None) -> list[str]:
    if plan is None:
        return ["no plan returned"]
    errors: list[str] = []
    if not plan.steps:
        errors.append("plan has no steps")
    if len(plan.steps) > MAX_PLAN_STEPS:
        errors.append(f"plan has more than {MAX_PLAN_STEPS} steps")
    step_ids = [step.step_id for step in plan.steps]
    if len(step_ids) != len(set(step_ids)):
        errors.append("duplicate step ids")
    known_ids = set(step_ids)
    for step in plan.steps:
        if step.agent not in _ALLOWED_AGENTS:
            errors.append(f"step {step.step_id} has an unknown agent {step.agent!r}")
        for dep in step.dependencies:
            if dep not in known_ids:
                errors.append(f"step {step.step_id} depends on unknown step {dep!r}")
    if not errors and _has_cycle(plan.steps):
        errors.append("plan dependency graph has a cycle")
    return errors


def _fallback_plan(query: str) -> PipelinePlan:
    return PipelinePlan(
        summary=f"Research: {query}",
        steps=[PipelineStep(step_id="step-1", agent="research", description=query, dependencies=[])],
        estimated_duration_seconds=120,
    )


def plan(state: PipelineState) -> dict[str, Any]:
    """Generate a step-by-step execution plan with dependencies and time estimates."""
    loader = PromptLoader(agent_templates_dir=_TEMPLATES_DIR)
    mode = state.get("mode", "science")
    query = last_human_message(state)
    system_prompt = loader.render("plan.j2", mode=mode)
    model = get_model().with_structured_output(PipelinePlan, include_raw=True)

    def _attempt(user_prompt: str) -> tuple[PipelinePlan | None, Any]:
        raw_result = model.invoke([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ])
        return raw_result.get("parsed"), raw_result.get("raw")

    trace: list[TraceEntry] = []
    total_in = total_out = 0

    parsed, raw = _attempt(f"Task: {query}")
    in_tok, out_tok = extract_tokens(raw) if raw is not None else (0, 0)
    total_in += in_tok
    total_out += out_tok
    errors = _validate_plan(parsed)

    if errors:
        trace.append(TraceEntry(stage="PLAN_INVALID", detail="; ".join(errors)))
        repaired_prompt = (
            f"Task: {query}\n\nThe previous plan was invalid: {'; '.join(errors)}. "
            "Return a corrected plan that fixes every issue."
        )
        parsed, raw = _attempt(repaired_prompt)
        in_tok, out_tok = extract_tokens(raw) if raw is not None else (0, 0)
        total_in += in_tok
        total_out += out_tok
        errors = _validate_plan(parsed)

    if errors:
        trace.append(TraceEntry(stage="PLAN_FALLBACK", detail="; ".join(errors)))
        parsed = _fallback_plan(query)

    assert parsed is not None
    trace.append(TraceEntry(
        stage="PLAN",
        detail=f"steps={len(parsed.steps)} | summary={parsed.summary}",
        input_tokens=total_in,
        output_tokens=total_out,
    ))

    steps = [StepStatus(step_id=step.step_id, agent=step.agent) for step in parsed.steps]
    logger.info({"event": "plan_complete", "steps": len(parsed.steps)})
    return {"plan": parsed, "steps": steps, "retry_count": 0, "trace": trace}


# ── approve ──────────────────────────────────────────────────────────────────


def _decision_action_id(decision: Any) -> str | None:
    if isinstance(decision, Mapping):
        return decision.get("action_id")
    return getattr(decision, "action_id", None)


def approve(state: PipelineState) -> dict[str, Any]:
    """Present the plan to the user via CEI UI ReviewGate and wait for approval."""
    plan_obj = state.get("plan")
    if plan_obj is None:
        return {"plan_approved": False}

    steps_input = [
        PlanStepInput(
            title=f"{step.step_id}: {step.agent}",
            description=(
                f"{step.description} (depends on: {', '.join(step.dependencies) or 'none'})"
            ),
        )
        for step in plan_obj.steps
    ]
    surface = build_plan_approval_surface(plan_obj.summary, steps_input)
    decision = interrupt(surface)
    approved = _decision_action_id(decision) == "approve-plan"
    logger.info({"event": "approve_complete", "approved": approved})
    return {"plan_approved": approved}


# ── dispatch ─────────────────────────────────────────────────────────────────


def _summarize(outcome_result: dict[str, Any] | None) -> str:
    if not outcome_result:
        return ""
    text = str(outcome_result.get("text", ""))
    return text[:_DEPENDENCY_SUMMARY_CHARS]


def _plain_detail(text: str | None, limit: int = 200) -> str:
    """One plain-text line for the job status table (no Markdown marks)."""
    plain = re.sub(r"[#*_`>|]+", " ", text or "")
    plain = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", plain)
    plain = " ".join(plain.split())
    return plain if len(plain) <= limit else plain[: limit - 1].rstrip() + "…"


async def dispatch(state: PipelineState, config: RunnableConfig) -> dict[str, Any]:
    """Launch sub-tasks on the appropriate agents (concurrent, sequential, or looped)."""
    plan_obj = state["plan"]
    steps_by_id = {step.step_id: step for step in plan_obj.steps}
    status_by_id = {status.step_id: status for status in state.get("steps", [])}
    session_id = state.get("session_id")
    if not session_id:  # no default session id: sub-step threads derive from the run's own session UUID
        raise ValueError("pipeline state has no session_id")
    mode = state.get("mode", "science")
    user_id = state.get("user_id", "cli-user")
    job_id = (config.get("configurable") or {}).get("job_id") if config else None

    retry_count = int(state.get("retry_count", 0))
    failed_ids = [sid for sid, status in status_by_id.items() if status.status == StepStatusValue.FAILED]
    is_retry = bool(failed_ids)
    if is_retry:
        retry_count += 1

    def _deps_satisfied(step: PipelineStep) -> bool:
        return all(
            status_by_id[dep].status in (StepStatusValue.COMPLETED, StepStatusValue.PARTIAL)
            for dep in step.dependencies
        )

    def _ready_idle() -> list[str]:
        return [
            sid
            for sid, status in status_by_id.items()
            if status.status == StepStatusValue.IDLE and _deps_satisfied(steps_by_id[sid])
        ]

    async def _run_one(sid: str, dependency_text: dict[str, str]) -> StepOutcome:
        step = steps_by_id[sid]
        dep_outputs = {dep: dependency_text[dep] for dep in step.dependencies if dep in dependency_text}
        return await _dispatcher.run(
            step, session_id=session_id, mode=mode, user_id=user_id, dependency_outputs=dep_outputs
        )

    # ADR: run wave after wave inside one dispatch | should_continue (unchanged) routes to
    # synthesize as soon as nothing is RUNNING or FAILED, so idle dependent steps would never run.
    trace: list[TraceEntry] = []
    ran: list[str] = []
    ready_ids = _ready_idle() + (failed_ids if is_retry else [])
    while ready_ids:
        dependency_text: dict[str, str] = {
            sid: _summarize(status.result)
            for sid, status in status_by_id.items()
            if status.status in (StepStatusValue.COMPLETED, StepStatusValue.PARTIAL)
        }
        for sid in ready_ids:
            status_by_id[sid].status = StepStatusValue.RUNNING
            if job_id:
                active_job_store().update_step(job_id, sid, "running")

        results = await asyncio.gather(*(_run_one(sid, dependency_text) for sid in ready_ids), return_exceptions=True)

        wave_failed = False
        for sid, result in zip(ready_ids, results):
            if isinstance(result, BaseException):
                outcome = StepOutcome(status="failed", error=repr(result))
            else:
                outcome = result
            status = status_by_id[sid]
            if outcome.status == "completed":
                status.status = StepStatusValue.COMPLETED
                status.error = None
            elif retry_count >= MAX_RETRIES:
                status.status = StepStatusValue.PARTIAL
                status.error = outcome.error or "step failed after retries"
            else:
                status.status = StepStatusValue.FAILED
                status.error = outcome.error or "step failed"
                wave_failed = True
            status.result = {"text": outcome.text, "document": outcome.document}
            trace.extend(outcome.trace)
            if job_id:
                active_job_store().update_step(
                    job_id, sid, status.status.value, error=status.error, detail=_plain_detail(outcome.text)
                )

        ran.extend(ready_ids)
        logger.info({"event": "dispatch_wave_complete", "ran": ready_ids, "retry_count": retry_count})
        if wave_failed:
            break  # should_continue routes back here to retry the failed steps
        ready_ids = _ready_idle()

    if not ran:
        return {"retry_count": retry_count}

    logger.info({"event": "dispatch_complete", "ran": ran, "retry_count": retry_count})
    return {"steps": list(status_by_id.values()), "retry_count": retry_count, "trace": trace}


# ── monitor ──────────────────────────────────────────────────────────────────


def monitor(state: PipelineState) -> dict[str, Any]:
    """Check step statuses and handle failures with retry/alternative/escalation.

    In-process dispatch (B95) runs each wave to completion inside ``dispatch``,
    so no step is ever left ``RUNNING`` when this node is reached. It still
    aggregates the step statuses for ``job_status`` polling and is the one
    place that can escalate a stuck run in the future dispatch mode.
    """
    steps = state.get("steps", [])
    retry_count = state.get("retry_count", 0)
    logger.info({
        "event": "monitor",
        "statuses": {status.step_id: status.status.value for status in steps},
        "retry_count": retry_count,
    })
    return {}


# ── synthesize ───────────────────────────────────────────────────────────────


def synthesize(state: PipelineState) -> dict[str, Any]:
    """Combine results from all completed steps into a unified output."""
    loader = PromptLoader(agent_templates_dir=_TEMPLATES_DIR)
    mode = state.get("mode", "science")
    plan_obj = state.get("plan")
    steps = state.get("steps", [])
    query = last_human_message(state)

    context_parts: list[str] = []
    for step in plan_obj.steps if plan_obj else []:
        status = next((s for s in steps if s.step_id == step.step_id), None)
        text = str(status.result.get("text", "")) if status and status.result else ""
        context_parts.append(f"### {step.step_id} ({step.agent}, status={status.status.value if status else 'idle'})\n{text}")

    context = "\n\n".join(context_parts)
    user_prompt = (
        f"Original task: {query}\n\nStep outputs:\n{context}\n\n"
        "Combine these into one unified answer. Note any step that ended partial or failed."
    )

    system_prompt = loader.system_prompt(mode)

    parsed, raw, error = invoke_structured(
        get_model(max_tokens=_SYNTHESIS_MAX_TOKENS),
        ResponseDocument,
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )

    trace: list[TraceEntry] = []
    if parsed is not None:
        try:
            corpus = build_corpus(query, *context_parts)
            doc, notes = ground_document(parsed, corpus)
            synthesis_text = render_document_markdown(doc)
            in_tok, out_tok = extract_tokens(raw) if raw is not None else (0, 0)
            trace.append(TraceEntry(
                stage="SYNTHESIZE",
                detail=f"mode={mode} | structured=true | response_length={len(synthesis_text)}",
                input_tokens=in_tok,
                output_tokens=out_tok,
            ))
            trace.extend(TraceEntry(stage="GROUNDING", detail=note) for note in notes)
            logger.info({"event": "synthesize_complete", "structured": True})
            return {
                "synthesis": synthesis_text,
                "response_document": doc,
                "messages": [AIMessage(content=synthesis_text)],
                "trace": trace,
            }
        except Exception as exc:  # ADR: any render/grounding failure falls back to prose | never lose the answer
            logger.error({"event": "synthesize_structured_render_error", "error_type": type(exc).__name__})
            error = type(exc).__name__

    trace.append(TraceEntry(stage="SYNTHESIZE_FALLBACK", detail=f"mode={mode} | error={error}"))
    model = get_model()
    response = model.invoke([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ])
    synthesis_text = str(response.content)
    in_tok, out_tok = extract_tokens(response)
    trace.append(TraceEntry(stage="SYNTHESIZE", detail=f"mode={mode} | response_length={len(synthesis_text)}", input_tokens=in_tok, output_tokens=out_tok))
    logger.info({"event": "synthesize_complete", "structured": False})
    return {
        "synthesis": synthesis_text,
        "response_document": None,
        "messages": [AIMessage(content=synthesis_text)],
        "trace": trace,
    }


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
