"""AgentCore Runtime entry point for the CEI Life Sciences Drafting Agent."""
from __future__ import annotations

import logging
import os
from collections import OrderedDict

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from drafting.state import DocumentType
from shared.presenters import build_drafting_surface
from shared.response_text import final_ai_text
from shared.runtime.history import begin_turn, end_turn
from shared.runtime.app_factory import create_app
from shared.runtime.identity import resolve_user_id

app = create_app()  # CORS for direct browser calls when CEI_ALLOWED_ORIGINS is set
log = app.logger
logging.basicConfig(level=logging.INFO)

_CHECKPOINT_LIMIT = 128
_checkpointer = InMemorySaver()
_thread_ids: OrderedDict[str, bool] = OrderedDict()


def _runtime_port() -> int:
    raw_port = os.environ.get("CEI_RUNTIME_PORT", "8080")
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


def _format_trace(trace_entries: list) -> str:
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


@app.entrypoint
async def invoke(payload, context):  # noqa: ANN001
    prompt = payload.get("prompt", "")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")

    doc_type_raw = payload.get("document_type", "")
    try:
        doc_type = DocumentType(doc_type_raw)
    except ValueError:
        raise ValueError(
            f"document_type must be one of: {', '.join(dt.value for dt in DocumentType)}"
        )

    session_id: str = getattr(context, "session_id", "default")
    mode: str = payload.get("mode", "science")
    user_id = resolve_user_id(payload, context)  # verified Cognito access-token sub; payload user_id ignored

    if mode not in ("science", "business"):
        mode = "science"

    _VALID_FORMATS = {"pdf", "docx", "md", "csv", "txt", "latex"}
    export_formats_raw = payload.get("export_formats", ["md", "txt"])
    export_formats = [f.lower() for f in export_formats_raw if f.lower() in _VALID_FORMATS]
    if not export_formats:
        export_formats = ["md", "txt"]

    from drafting.graph import compile_graph

    # Cold thread (runtime restart / LRU eviction): rebuild short-term memory from ChatHistoryStore
    history = await begin_turn(
        user_id=user_id,
        session_id=session_id,
        prompt=prompt,
        agent_type="drafting",
        document_type=doc_type.value,
        mode=mode,
        warm=session_id in _thread_ids,
        saver=_checkpointer,
    )
    _touch_thread(session_id)
    graph = compile_graph(checkpointer=_checkpointer)

    initial_state = {
        # D16: history is dynamic content — it rides in messages, after the cached system prompt/tools
        "messages": [*history, HumanMessage(content=prompt)],
        "mode": mode,
        "user_id": user_id,
        "session_id": session_id,
        "document_type": doc_type,
        "guidelines": [],
        "research_questions": [],
        "research_findings": [],
        "draft": "",
        "draft_document": None,
        "revision_feedback": None,
        "trace": [],
        "export_formats": export_formats,
        "chart_specs": [],
        "chart_data": {},
        "chart_instructions": None,
        "exports": [],
    }

    log.info(f"Drafting agent invoked: session={session_id}, mode={mode}, doc_type={doc_type.value}")

    result = await graph.ainvoke(
        initial_state,
        config={"configurable": {"thread_id": session_id}},
    )

    text = final_ai_text(result.get("messages", []))
    if text is None:
        text = "No response generated."
        response = {
            "result": text,
            "text": text,
            "ui": build_drafting_surface(
                text,
                surface_id="drafting-final",
                exports=[],
                chart_specs=[],
                chart_instructions=None,
                document=result.get("draft_document"),
            ).model_dump(mode="json"),
            "exports": [],
        }
        await end_turn(saver=_checkpointer, user_id=user_id, session_id=session_id, envelope=response, prompt=prompt)
        return response

    trace_text = _format_trace(result.get("trace", []))
    full_output = text + trace_text

    exports = [
        {key: export_item[key] for key in ("format", "filename", "byte_size") if key in export_item}
        for export_item in result.get("exports", [])
    ]

    log.info(f"Drafting agent complete: session={session_id}, length={len(text)}, exports={len(exports)}")
    response = {
        "result": full_output,
        "text": text,
        "ui": build_drafting_surface(
            text,
            surface_id="drafting-final",
            exports=exports,
            chart_specs=result.get("chart_specs", []),
            chart_instructions=result.get("chart_instructions"),
            document=result.get("draft_document"),
        ).model_dump(mode="json"),
        "exports": exports,
    }
    # stores `text` (not `result`): the trace is diagnostic output, not conversation content
    await end_turn(saver=_checkpointer, user_id=user_id, session_id=session_id, envelope=response, prompt=prompt)
    return response


if __name__ == "__main__":
    app.run(port=_runtime_port())
