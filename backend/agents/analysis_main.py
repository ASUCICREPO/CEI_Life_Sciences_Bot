"""AgentCore Runtime entry point for the CEI Life Sciences Analysis Agent (B94)."""
from __future__ import annotations

from shared.telemetry import configure_telemetry
configure_telemetry()

import logging
import os
from collections import OrderedDict
from typing import Any, Dict, List

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from analysis.state import AnalysisType
from shared.presenters import build_analysis_surface
from shared.response_text import final_ai_text
from shared.runtime import (
    PendingInterrupts,
    ReplayHub,
    RuntimeOp,
    action_response,
    awaiting_envelope,
    completed_envelope,
    extract_interrupt_surface,
    parse_mode,
    parse_op,
    parse_prompt,
    resume_value,
)
from shared.runtime.history import begin_turn, end_turn, record_resume
from shared.runtime.hitl_turn import resolve_resume_action
from shared.runtime.app_factory import create_app
from shared.streaming.trace_redaction import redacts_trace_input
from shared.runtime.session_guard import AuthorizedSession, guarded_entrypoint

app = create_app()  # CORS for direct browser calls when CEI_ALLOWED_ORIGINS is set
log = app.logger
logging.basicConfig(level=logging.INFO)

_CHECKPOINT_LIMIT = 128
_checkpointer = InMemorySaver()
_thread_ids: "OrderedDict[str, bool]" = OrderedDict()
_pending = PendingInterrupts()
_replay = ReplayHub.for_agent("analysis")
# Replay scenario key per thread: "<analysis_type>|<prompt>" (B94 replay mode).
_thread_scenarios: "OrderedDict[str, str]" = OrderedDict()


def _remember_scenario(thread_id: str, scenario: str) -> None:
    _thread_scenarios[thread_id] = scenario
    _thread_scenarios.move_to_end(thread_id)
    while len(_thread_scenarios) > _CHECKPOINT_LIMIT:
        _thread_scenarios.popitem(last=False)


def _runtime_port() -> int:
    raw_port = os.environ.get("CEI_RUNTIME_PORT", "8080")  # AgentCore Runtime sends traffic to 8080 only
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
    _thread_ids[thread_id] = True


@redacts_trace_input  # B7: model ids / guardrails / prompts / secrets never reach trace_text
def _format_trace(trace_entries: List[Any]) -> str:
    if not trace_entries:
        return ""
    lines = ["", "═══ AGENT TRACE ═══"]
    total_in = 0
    total_out = 0
    for entry in trace_entries:
        tag = entry.stage if isinstance(entry.stage, str) else entry["stage"]
        detail = entry.detail if isinstance(entry.detail, str) else entry["detail"]
        in_tok = entry.input_tokens if hasattr(entry, "input_tokens") else entry.get("input_tokens", 0)
        out_tok = entry.output_tokens if hasattr(entry, "output_tokens") else entry.get("output_tokens", 0)
        total_in += in_tok
        total_out += out_tok

        token_str = ""
        if in_tok or out_tok:
            token_str = f"  [tokens: {in_tok}→in, {out_tok}→out]"
        lines.append(f"  [{tag}] {detail}{token_str}")

    lines.append(f"  [TOTAL TOKENS] input={total_in} | output={total_out} | total={total_in + total_out}")
    lines.append("═══════════════════")
    return "\n".join(lines)


def _parse_analysis_type(raw: Any) -> AnalysisType | None:
    """Normalize a payload analysis_type: a valid AnalysisType, otherwise None."""
    if not raw:
        return None
    try:
        return AnalysisType(raw)
    except ValueError:
        return None


async def _analysis_long_term_context(user_id: str, prompt: str) -> list[str]:
    """Load only this actor's memory when the opt-in read path is enabled (F11)."""
    from shared.memory.turns import load_long_term_context

    return await load_long_term_context(
        user_id, prompt, enabled_env="CEI_ANALYSIS_LONG_TERM_ENABLED", memory_id_env="CEI_ANALYSIS_MEMORY_ID",
    )


def _initial_state(
    prompt: str, mode: str, user_id: str, session_id: str, analysis_type: str | None,
    long_term_context: list[str] = (),
) -> Dict[str, Any]:
    parsed_type = _parse_analysis_type(analysis_type)
    return {
        "messages": [HumanMessage(content=prompt)],
        "mode": mode,
        "long_term_context": list(long_term_context),
        "user_id": user_id,
        "session_id": session_id,
        "query": prompt,
        "analysis_type": parsed_type,
        "competitors": [],
        "market": None,
        "jurisdiction": None,
        "focal_entity": None,
        "confirmed_scope": False,
        "scope_revision": 0,
        "scope_correction": None,
        "retrieved_results": [],
        "findings": [],
        "comparisons": [],
        "report": "",
        "response_document": None,
        "trace": [],
    }


def _completed_result(session_id: str, result: Dict[str, Any]) -> Dict[str, Any]:
    text = final_ai_text(result.get("messages", []))
    if text is None:
        text = "No response generated."
        return completed_envelope(
            text,
            build_analysis_surface(
                text,
                surface_id="analysis-final",
                retrieved_results=result.get("retrieved_results", []),
                document=result.get("response_document"),
            ).model_dump(mode="json"),
        )

    trace_text = _format_trace(result.get("trace", []))
    log.info(f"Analysis agent complete: session={session_id}, length={len(text)}")
    return completed_envelope(
        text,
        build_analysis_surface(
            text,
            surface_id="analysis-final",
            retrieved_results=result.get("retrieved_results", []),
            document=result.get("response_document"),
        ).model_dump(mode="json"),
        trace_text=trace_text,
    )


@app.entrypoint
@guarded_entrypoint  # UUID session id + verified caller + ownership, before any op touches the session
async def invoke(payload, context, session: AuthorizedSession):  # noqa: ANN001
    from analysis.graph import compile_graph

    session_id = session.session_id
    user_id = session.user_id
    op = parse_op(payload)

    if op is RuntimeOp.JOB_STATUS:
        raise ValueError("job_status is not supported by the Analysis runtime")

    graph = compile_graph(checkpointer=_checkpointer)
    config = {"configurable": {"thread_id": session_id}}

    if op is RuntimeOp.RESUME:
        scenario = _thread_scenarios.get(session_id, "")
        action, terminal = await resolve_resume_action(
            payload, session_id=session_id, user_id=user_id, checkpointer=_checkpointer, graph=graph,
            pending=_pending, is_cold=session_id not in _thread_ids, replay=_replay, replay_key=scenario,
        )
        if terminal is not None:
            return terminal

        await record_resume(user_id=user_id, session_id=session_id, action=action)
        _touch_thread(session_id)
        result = await graph.ainvoke(Command(resume=resume_value(action)), config=config)
        surface = extract_interrupt_surface(result)
        response = action_response(action, "completed")

        if surface is not None:
            _pending.register(session_id, surface)
            envelope = awaiting_envelope(surface, "Waiting for scope confirmation.")
        else:
            _pending.clear(session_id)
            envelope = _completed_result(session_id, result)

        envelope["action_response"] = response
        await end_turn(
            saver=_checkpointer,
            user_id=user_id,
            session_id=session_id,
            envelope=envelope,
            retrieved_results=result.get("retrieved_results"),
            trace=result.get("trace"),
        )
        if _replay.mode.value == "record" and scenario:
            _replay.record_resume(scenario, envelope)
        return envelope

    prompt = parse_prompt(payload)
    mode = parse_mode(payload)
    analysis_type = payload.get("analysis_type")
    scenario = f"{analysis_type or 'auto'}|{prompt}"
    parsed_analysis_type = _parse_analysis_type(analysis_type)
    _remember_scenario(session_id, scenario)

    if _replay.enabled:
        replayed = _replay.on_run(scenario, session_id=session_id)
        if replayed is not None:
            return replayed

    # New run only: a resume (handled above) must not re-recall long-term memory.
    memories = await _analysis_long_term_context(user_id, prompt)

    # Cold thread (runtime restart / LRU eviction): rebuild short-term memory from ChatHistoryStore
    history = await begin_turn(
        user_id=user_id,
        session_id=session_id,
        prompt=prompt,
        agent_type="analysis",
        analysis_type=parsed_analysis_type.value if parsed_analysis_type else None,
        mode=mode,
        warm=session_id in _thread_ids,
        saver=_checkpointer,
    )
    _touch_thread(session_id)
    log.info(f"Analysis agent invoked: session={session_id}, mode={mode}, analysis_type={analysis_type}")

    state = _initial_state(prompt, mode, user_id, session_id, analysis_type, memories)
    # D16: history is dynamic content — it rides in messages, after the cached system prompt/tools
    state["messages"] = [*history, *state["messages"]]
    result = await graph.ainvoke(state, config=config)
    surface = extract_interrupt_surface(result)
    if surface is not None:
        _pending.register(session_id, surface)
        envelope = awaiting_envelope(surface, "Confirm the analysis scope before proceeding.")
    else:
        envelope = _completed_result(session_id, result)
    await end_turn(
        saver=_checkpointer,
        user_id=user_id,
        session_id=session_id,
        envelope=envelope,
        prompt=prompt,
        retrieved_results=result.get("retrieved_results"),
        trace=result.get("trace"),
    )
    if _replay.mode.value == "record":
        _replay.record_run(scenario, envelope)
    return envelope


if __name__ == "__main__":
    app.run(port=_runtime_port())
