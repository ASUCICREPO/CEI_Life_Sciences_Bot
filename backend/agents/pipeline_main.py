"""AgentCore Runtime entry point for the CEI Pipeline Manager (B94, B96, B98)."""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections import OrderedDict
from typing import Any, Dict, List

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from shared.presenters.pipeline import build_pipeline_status_surface, build_pipeline_surface
from shared.response_text import final_ai_text
from shared.runtime import (
    JOB_REGISTRY,
    JobStatus,
    PendingInterrupts,
    ReplayHub,
    RuntimeOp,
    action_response,
    awaiting_envelope,
    check_resume,
    completed_envelope,
    extract_interrupt_surface,
    parse_action,
    parse_mode,
    parse_op,
    parse_prompt,
    resume_value,
)
from shared.runtime.history import begin_turn, end_turn, record_resume
from shared.runtime.checkpointing import restore_for_resume
from shared.runtime.app_factory import create_app
from shared.runtime.session_guard import AuthorizedSession, guarded_entrypoint

app = create_app()  # CORS for direct browser calls when CEI_ALLOWED_ORIGINS is set
log = app.logger
logging.basicConfig(level=logging.INFO)

# ADR: status surfaces come from this module | B98 adds steps[]/ui to job_status
JOB_REGISTRY.status_ui_builder = build_pipeline_status_surface

_CHECKPOINT_LIMIT = 128
_checkpointer = InMemorySaver()
_thread_ids: OrderedDict[str, bool] = OrderedDict()
_thread_prompts: OrderedDict[str, str] = OrderedDict()
_pending = PendingInterrupts()
_replay = ReplayHub.for_agent("pipeline")
_background_tasks: Dict[str, asyncio.Task] = {}


def _runtime_port() -> int:
    raw_port = os.environ.get("CEI_RUNTIME_PORT", "8084")
    try:
        port = int(raw_port)
    except ValueError as error:
        raise ValueError("CEI_RUNTIME_PORT must be an integer TCP port.") from error
    if not 1 <= port <= 65535:
        raise ValueError("CEI_RUNTIME_PORT must be between 1 and 65535.")
    return port


def _touch_thread(thread_id: str) -> None:
    if thread_id in _thread_ids:
        _thread_ids.move_to_end(thread_id)
        return
    while len(_thread_ids) >= _CHECKPOINT_LIMIT:
        evicted, _ = _thread_ids.popitem(last=False)
        _checkpointer.delete_thread(evicted)
        _thread_prompts.pop(evicted, None)
    _thread_ids[thread_id] = True


def _format_trace(trace_entries: List[Any]) -> str:
    if not trace_entries:
        return ""
    lines = ["", "═══ AGENT TRACE ═══"]
    total_in = 0
    total_out = 0
    for entry in trace_entries:
        tag = entry.stage if hasattr(entry, "stage") else entry["stage"]
        detail = entry.detail if hasattr(entry, "detail") else entry["detail"]
        in_tok = entry.input_tokens if hasattr(entry, "input_tokens") else entry.get("input_tokens", 0)
        out_tok = entry.output_tokens if hasattr(entry, "output_tokens") else entry.get("output_tokens", 0)
        total_in += in_tok
        total_out += out_tok
        token_str = f"  [tokens: {in_tok}→in, {out_tok}→out]" if (in_tok or out_tok) else ""
        lines.append(f"  [{tag}] {detail}{token_str}")
    lines.append(f"  [TOTAL TOKENS] input={total_in} | output={total_out} | total={total_in + total_out}")
    lines.append("═══════════════════")
    return "\n".join(lines)


def _compile_graph():
    from pipeline_manager.graph import compile_graph

    return compile_graph(checkpointer=_checkpointer)


def _plan_steps_for_registry(plan_obj: Any) -> list[tuple[str, str, str]]:
    return [(step.step_id, step.agent, step.description) for step in plan_obj.steps]


def _step_outcomes(steps: list) -> list[dict[str, Any]]:
    outcomes = []
    for status in steps:
        result = status.result or {}
        outcomes.append({
            "step_id": status.step_id,
            "agent": status.agent,
            "status": status.status.value if hasattr(status.status, "value") else status.status,
            "text": result.get("text", ""),
            "document": result.get("document"),
        })
    return outcomes


def _final_job_status(steps: list) -> JobStatus:
    statuses = {status.status.value if hasattr(status.status, "value") else status.status for status in steps}
    if not statuses or statuses == {"completed"}:
        return JobStatus.COMPLETED
    if "completed" not in statuses and "partial" not in statuses:
        return JobStatus.FAILED
    return JobStatus.PARTIAL


async def _run_job(job_id: str, session_id: str, user_id: str, action_resume_value: dict, task_id: int) -> None:
    try:
        graph = _compile_graph()
        result = await graph.ainvoke(
            Command(resume=action_resume_value),
            config={"configurable": {"thread_id": session_id, "job_id": job_id}},
        )
        steps = result.get("steps", [])
        text = result.get("synthesis") or final_ai_text(result.get("messages", [])) or ""
        ui = build_pipeline_surface(
            text,
            surface_id="pipeline-final",
            document=result.get("response_document"),
            step_outcomes=_step_outcomes(steps),
        ).model_dump(mode="json")
        JOB_REGISTRY.finish(job_id, _final_job_status(steps), ui=ui, text=text)
        # the async job's final answer is the AI turn that closes this conversation step
        await end_turn(saver=_checkpointer, user_id=user_id, session_id=session_id, envelope={"text": text, "ui": ui})
        prompt = _thread_prompts.get(session_id, "")
        if prompt:
            _replay.record_job_status(prompt, JOB_REGISTRY.snapshot(job_id))
        log.info(f"Pipeline job complete: job={job_id} session={session_id}")
    except Exception as exc:  # ADR: a crashed background job must still resolve the job record | never hang /pipeline
        log.error(f"Pipeline job failed: job={job_id} session={session_id} error={exc!r}")
        JOB_REGISTRY.finish(job_id, JobStatus.FAILED, ui=None, text=f"Pipeline run failed: {exc!r}")
        await end_turn(saver=_checkpointer, user_id=user_id, session_id=session_id, envelope={"text": "Pipeline run failed."})
    finally:
        app.complete_async_task(task_id)
        _background_tasks.pop(job_id, None)


@app.entrypoint
@guarded_entrypoint  # UUID session id + verified caller + ownership, before any op touches the session
async def invoke(payload, context, session: AuthorizedSession):  # noqa: ANN001
    session_id = session.session_id
    user_id = session.user_id
    op = parse_op(payload)

    if op is RuntimeOp.JOB_STATUS:
        job_id = payload.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            return {"status": "error", "message": "job_id is required."}
        if _replay.enabled:
            # replayed jobs are fixtures, not registry records; the session itself was owner-checked above
            replayed = _replay.on_job_status(job_id, session_id=session_id)
            if replayed is not None:
                return replayed
        # the job must have been started by this caller in this session (jobs without an owner are refused)
        if JOB_REGISTRY.get_owned(job_id, user_id=user_id, session_id=session_id) is None:
            log.warning(f"Job status refused: job={job_id} session={session_id}")
            return {"status": "error", "message": "Job not found."}
        snapshot = JOB_REGISTRY.snapshot(job_id)
        if _replay.mode.value == "record":
            # Each distinct poll snapshot is recorded, so replay shows the live step progress.
            _replay.record_job_status(_thread_prompts.get(session_id, ""), snapshot)
        return snapshot

    if op is RuntimeOp.RESUME:
        action = parse_action(payload)
        prompt = _thread_prompts.get(session_id, "")

        if _replay.enabled:
            replayed = _replay.on_resume(prompt, session_id=session_id, action=action, job_id=None)
            if replayed is not None:
                return replayed

        if session_id not in _thread_ids and _pending.get(session_id) is None and action.session_id == session_id:
            # cold resume (runtime restart / eviction): bring back the interrupted graph + its pending surface
            await restore_for_resume(
                user_id=user_id, session_id=session_id, saver=_checkpointer, graph=_compile_graph(), pending=_pending
            )
        terminal = check_resume(_pending, session_id, action)
        if terminal is not None:
            return terminal
        await record_resume(user_id=user_id, session_id=session_id, action=action)
        _pending.clear(session_id)

        graph = _compile_graph()
        _touch_thread(session_id)
        config = {"configurable": {"thread_id": session_id}}

        if action.action_id != "approve-plan":
            # reject-plan (or any other target action on this surface) ends the run.
            await graph.ainvoke(Command(resume=resume_value(action)), config=config)
            text = "Plan rejected."
            envelope = completed_envelope(text, None, extra={"action_response": action_response(action, "completed")})
            await end_turn(saver=_checkpointer, user_id=user_id, session_id=session_id, envelope=envelope)
            if _replay.mode.value == "record":
                _replay.record_resume(prompt, envelope)
            return envelope

        snapshot = await graph.aget_state(config)
        plan_obj = snapshot.values.get("plan") if snapshot else None
        if plan_obj is None:
            return {"status": "error", "message": "No pending plan for this session."}

        job_id = f"job-{uuid.uuid4().hex[:12]}"
        JOB_REGISTRY.create(
            session_id=session_id,
            agent_type="pipeline_manager",
            steps=_plan_steps_for_registry(plan_obj),
            job_id=job_id,
            user_id=user_id,
        )
        JOB_REGISTRY.set_status(job_id, JobStatus.RUNNING)

        task_id = app.add_async_task("pipeline", {"job_id": job_id, "session_id": session_id})
        task = asyncio.create_task(_run_job(job_id, session_id, user_id, resume_value(action), task_id))
        _background_tasks[job_id] = task

        text = "Plan approved — running the pipeline."
        envelope = {
            "job_id": job_id,
            "status": "running",
            "action_response": action_response(action, "accepted"),
            "text": text,
            "result": text,
        }
        await end_turn(saver=_checkpointer, user_id=user_id, session_id=session_id, envelope=envelope)
        if _replay.mode.value == "record":
            _replay.record_resume(prompt, envelope)
        return envelope

    # op is RUN (default)
    prompt = parse_prompt(payload)
    mode = parse_mode(payload)
    _thread_prompts[session_id] = prompt

    if _replay.enabled:
        replayed = _replay.on_run(prompt, session_id=session_id)
        if replayed is not None:
            return replayed

    graph = _compile_graph()
    # Cold thread (runtime restart / LRU eviction): rebuild short-term memory from ChatHistoryStore
    history = await begin_turn(
        user_id=user_id,
        session_id=session_id,
        prompt=prompt,
        agent_type="pipeline_manager",
        mode=mode,
        warm=session_id in _thread_ids,
        saver=_checkpointer,
    )
    _touch_thread(session_id)
    initial_state = {
        # D16: history is dynamic content — it rides in messages, after the cached system prompt/tools
        "messages": [*history, HumanMessage(content=prompt)],
        "mode": mode,
        "user_id": user_id,
        "session_id": session_id,
        "plan": None,
        "plan_approved": False,
        "steps": [],
        "synthesis": "",
        "retry_count": 0,
        "response_document": None,
        "trace": [],
    }

    log.info(f"Pipeline manager invoked: session={session_id}, mode={mode}")

    result = await graph.ainvoke(initial_state, config={"configurable": {"thread_id": session_id}})
    surface = extract_interrupt_surface(result)
    if surface is None:
        text = "The plan could not be built."
        envelope = completed_envelope(text, None, trace_text=_format_trace(result.get("trace", [])))
        await end_turn(saver=_checkpointer, user_id=user_id, session_id=session_id, envelope=envelope, prompt=prompt)
        if _replay.mode.value == "record":
            _replay.record_run(prompt, envelope)
        return envelope

    _pending.register(session_id, surface)
    plan_obj = result.get("plan")
    text = f"## Proposed plan\n\n{plan_obj.summary}" if plan_obj else "A plan is ready for approval."
    envelope = awaiting_envelope(surface, text)
    await end_turn(saver=_checkpointer, user_id=user_id, session_id=session_id, envelope=envelope, prompt=prompt)
    if _replay.mode.value == "record":
        _replay.record_run(prompt, envelope)
    return envelope


if __name__ == "__main__":
    app.run(port=_runtime_port())
