"""AgentCore Runtime entry point for the CEI Life Sciences Analysis Agent (B94)."""
from __future__ import annotations

import logging
import os
from collections import OrderedDict
from typing import Any, Dict, List

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from analysis.state import AnalysisType
from shared.presenters import build_analysis_surface
from shared.response_text import final_ai_text
from shared.runtime import (
    PendingInterrupts,
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

app = BedrockAgentCoreApp()
log = app.logger
logging.basicConfig(level=logging.INFO)

_CHECKPOINT_LIMIT = 128
_checkpointer = InMemorySaver()
_thread_ids: "OrderedDict[str, bool]" = OrderedDict()
_pending = PendingInterrupts()


def _runtime_port() -> int:
    raw_port = os.environ.get("CEI_RUNTIME_PORT", "8083")
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


def _initial_state(prompt: str, mode: str, user_id: str, session_id: str, analysis_type: str | None) -> Dict[str, Any]:
    parsed_type: AnalysisType | None = None
    if analysis_type:
        try:
            parsed_type = AnalysisType(analysis_type)
        except ValueError:
            parsed_type = None
    return {
        "messages": [HumanMessage(content=prompt)],
        "mode": mode,
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
async def invoke(payload, context):  # noqa: ANN001
    from analysis.graph import compile_graph

    session_id: str = getattr(context, "session_id", "default")
    op = parse_op(payload)

    if op is RuntimeOp.JOB_STATUS:
        raise ValueError("job_status is not supported by the Analysis runtime")

    graph = compile_graph(checkpointer=_checkpointer)
    config = {"configurable": {"thread_id": session_id}}

    if op is RuntimeOp.RESUME:
        action = parse_action(payload)
        stale = check_resume(_pending, session_id, action)
        if stale is not None:
            return stale

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
        return envelope

    prompt = parse_prompt(payload)
    mode = parse_mode(payload)
    user_id: str = payload.get("user_id", "cli-user")
    analysis_type = payload.get("analysis_type")

    _touch_thread(session_id)
    log.info(f"Analysis agent invoked: session={session_id}, mode={mode}, analysis_type={analysis_type}")

    result = await graph.ainvoke(_initial_state(prompt, mode, user_id, session_id, analysis_type), config=config)
    surface = extract_interrupt_surface(result)
    if surface is not None:
        _pending.register(session_id, surface)
        return awaiting_envelope(surface, "Confirm the analysis scope before proceeding.")

    return _completed_result(session_id, result)


if __name__ == "__main__":
    app.run(port=_runtime_port())
