"""AgentCore Runtime entry point for the CEI Life Sciences Drafting Agent."""
from __future__ import annotations

from shared.telemetry import configure_telemetry
configure_telemetry()

import asyncio
import logging
import os
from collections import OrderedDict
from collections.abc import Mapping as MappingABC
from functools import lru_cache
from typing import Any, Dict

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from starlette.responses import StreamingResponse

from drafting.state import DocumentType
from shared.presenters import build_drafting_surface
from shared.response_text import final_ai_text
from shared.runtime import (
    PendingInterrupts,
    RuntimeOp,
    action_response,
    awaiting_envelope,
    extract_interrupt_surface,
    parse_op,
    resume_value,
)
from shared.runtime.hitl_turn import resolve_resume_action
from shared.runtime.history import begin_turn, end_turn, record_resume
from shared.runtime.app_factory import create_app
from shared.streaming.research import ResearchEventStream, text_chunks, with_heartbeats
from shared.streaming.trace_redaction import redacts_trace_input
from shared.runtime.session_guard import AuthorizedSession, guarded_entrypoint
from shared.tools.gateway_context import gateway_request_context

app = create_app()  # CORS for direct browser calls when CEI_ALLOWED_ORIGINS is set
log = app.logger
logging.basicConfig(level=logging.INFO)

_CHECKPOINT_LIMIT = 128
_checkpointer = InMemorySaver()
_thread_ids: OrderedDict[str, bool] = OrderedDict()
_pending = PendingInterrupts()

_VALID_FORMATS = {"pdf", "docx", "md", "csv", "txt", "latex"}

# route_request's node name maps to the user-facing step label streamed as `progress` (B1xx).
_NODE_PROGRESS_LABELS: dict[str, str] = {
    "route_request": "Understanding the request",
    "research_guidelines": "Checking guidelines",
    "plan_research": "Planning the research",
    "research_subagent": "Gathering evidence",
    "outline": "Writing the outline",
    "approve_outline": "Preparing the outline for review",
    "draft": "Writing the draft",
    "revise": "Revising the draft",
    "plan_visuals": "Preparing charts",
    "export_documents": "Creating files",
    "chat_answer": "Preparing the answer",
}
# route_request, draft, draft_document, document_type, outline(+revision/approval/correction),
# guidelines, research_findings, chart data, and the last exports/formats all carry forward across
# turns on the same thread instead of resetting every run (today's bug: every turn re-ran the
# outline, even a follow-up).
_CARRY_FORWARD_KEYS = (
    "draft", "draft_document", "document_type", "outline", "outline_revision", "outline_approved",
    "outline_correction", "needs_outline_approval", "guidelines", "research_findings", "chart_specs",
    "chart_data", "chart_instructions", "exports", "export_formats",
)


async def _drafting_long_term_context(user_id: str, prompt: str) -> list[str]:
    """Load only this actor's memory when the opt-in read path is enabled (F11)."""
    from shared.memory.turns import load_long_term_context

    return await load_long_term_context(
        user_id, prompt, enabled_env="CEI_DRAFTING_LONG_TERM_ENABLED", memory_id_env="CEI_DRAFTING_MEMORY_ID",
    )


@lru_cache(maxsize=1)
def _persistent_drafting_checkpointer():
    """AgentCoreMemorySaver whenever an AgentCore Memory ID is configured (every deployed runtime, CDK);
    a local run with no Memory ID keeps its in-process InMemorySaver. Same rule as Research.
    """
    memory_id = os.environ.get("CEI_DRAFTING_MEMORY_ID", "").strip()
    if not memory_id:
        return None
    from shared.memory.agentcore import create_checkpointer

    return create_checkpointer(memory_id)


async def _drafting_checkpoint_context(user_id: str, session_id: str):
    """Choose the saver and detect an existing actor-scoped persistent checkpoint (mirrors main.py's
    `_research_checkpoint_context`). `warm=True` here also drives carry-forward: an existing persistent
    checkpoint already holds the prior draft/outline state (`_load_warm_carry_forward`'s `aget_state`),
    so a first turn after migration is the only case that needs the cold `md`-export recovery.

    Memory failures propagate instead of silently falling back across sessions.
    """
    persistent = _persistent_drafting_checkpointer()
    if persistent is None:
        return _checkpointer, session_id in _thread_ids, False
    config = {"configurable": {"thread_id": session_id, "actor_id": user_id}}
    checkpoint = await persistent.aget_tuple(config)
    return persistent, checkpoint is not None, True


async def _restore_from_memory(*, user_id, session_id, saver, graph, pending) -> bool:
    """B82 expiry check for the AgentCoreMemorySaver path (mirrors main.py's Research equivalent).

    The persistent saver already *is* the durable store across processes; `aget_tuple` tells us
    whether AgentCore Memory's own short-term event expiry has already removed the checkpoint.
    """
    config = {"configurable": {"thread_id": session_id, "actor_id": user_id}}
    checkpoint = await saver.aget_tuple(config)
    if checkpoint is None:
        return False
    snapshot = await graph.aget_state(config)
    interrupts = list(getattr(snapshot, "interrupts", None) or [])
    if not interrupts:
        for task in getattr(snapshot, "tasks", None) or []:
            interrupts.extend(getattr(task, "interrupts", None) or [])
    surface = extract_interrupt_surface({"__interrupt__": interrupts})
    if isinstance(surface, MappingABC):
        pending.register(session_id, surface)
    return True


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


async def _load_warm_carry_forward(graph: Any, config: dict) -> dict[str, Any]:
    """The prior checkpoint's draft/outline/etc, for a warm thread (same in-process runtime).

    Best-effort: any failure (including a stub/mock graph in a test) carries nothing forward
    rather than failing the turn.
    """
    try:
        snapshot = await graph.aget_state(config)
    except Exception as exc:  # noqa: BLE001
        log.error("drafting carry-forward: aget_state failed error_type=%s", type(exc).__name__)
        return {}
    values = getattr(snapshot, "values", None)
    if not isinstance(values, dict):
        return {}
    return {key: values[key] for key in _CARRY_FORWARD_KEYS if key in values}


async def _load_cold_carry_forward(user_id: str, session_id: str) -> dict[str, Any]:
    """Runtime restart/eviction: recover only the last draft from the newest `md` export, if any."""
    from drafting.nodes import _export_repository

    repository = _export_repository()
    if repository is None:
        return {}
    try:
        page = await asyncio.to_thread(repository.list_for_session, user_id, session_id, limit=20)
        md_items = [item for item in page.items if item.get("format") == "md" and item.get("status") == "ready"]
        if not md_items:
            return {}
        latest = max(md_items, key=lambda item: item.get("created_at", ""))
        data = await asyncio.to_thread(repository.download_bytes, latest)
        return {
            "draft": data.decode("utf-8"),
            "draft_document": None,
            "exports": [{
                "export_id": latest["export_id"], "format": "md",
                "filename": latest["filename"], "byte_size": latest.get("size", 0),
            }],
        }
    except Exception as exc:  # noqa: BLE001
        log.error("drafting carry-forward: cold recovery failed session=%s error_type=%s", session_id, type(exc).__name__)
        return {}


@redacts_trace_input  # B7: model ids / guardrails / prompts / secrets never reach trace_text
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


def _public_exports(result: Dict[str, Any]) -> list[dict]:
    return [
        {key: export_item[key] for key in ("export_id", "format", "filename", "byte_size") if key in export_item}
        for export_item in result.get("exports", [])
    ]


def _drafting_envelope(session_id: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """Build the completed-turn envelope for a resumed drafting run (mirrors the `op=run` shape)."""
    text = final_ai_text(result.get("messages", []))
    if text is None:
        text = "No response generated."
        return {
            "result": text,
            "text": text,
            "ui": build_drafting_surface(
                text, surface_id="drafting-final", exports=[], chart_specs=[],
                chart_instructions=None, document=result.get("draft_document"),
            ).model_dump(mode="json"),
            "exports": [],
        }
    trace_text = _format_trace(result.get("trace", []))
    exports = _public_exports(result)
    log.info(f"Drafting agent complete (resume): session={session_id}, length={len(text)}, exports={len(exports)}")
    return {
        "result": text + trace_text,
        "text": text,
        "ui": build_drafting_surface(
            text, surface_id="drafting-final", exports=exports,
            chart_specs=result.get("chart_specs", []), chart_instructions=result.get("chart_instructions"),
            document=result.get("draft_document"),
        ).model_dump(mode="json"),
        "exports": exports,
    }


async def _resume(payload, context, session_id: str, user_id: str) -> Dict[str, Any]:
    """`op: resume` (F13): pause/resume for the outline-approval HITL point, same pattern as analysis_main.

    With the persistent AgentCoreMemorySaver (opt-in), mirrors main.py's Research `_resume`: an
    actor-scoped config, `_restore_from_memory`'s aget_tuple-based expiry check in place of the
    default ChatHistoryStore-snapshot restore, and an ephemeral `pending` registry (a persistent
    session re-derives its pending surface from the checkpoint on every cold resume).
    """
    from drafting.graph import compile_graph

    persistent_saver = _persistent_drafting_checkpointer()
    persistent = persistent_saver is not None
    saver = persistent_saver if persistent else _checkpointer
    graph = compile_graph(checkpointer=saver)
    pending = PendingInterrupts() if persistent else _pending
    restore = _restore_from_memory if persistent else None
    kwargs: Dict[str, Any] = {}
    if restore is not None:
        kwargs["restore"] = restore

    action, terminal = await resolve_resume_action(
        payload, session_id=session_id, user_id=user_id, checkpointer=saver, graph=graph,
        pending=pending, is_cold=True if persistent else session_id not in _thread_ids, **kwargs,
    )
    if terminal is not None:
        return terminal

    await record_resume(user_id=user_id, session_id=session_id, action=action)
    config = {"configurable": {"thread_id": session_id, "actor_id": user_id}}
    if not persistent:
        _touch_thread(session_id)
    with gateway_request_context(context, user_id, session_id):
        result = await graph.ainvoke(Command(resume=resume_value(action)), config=config)

    surface = extract_interrupt_surface(result)
    response = action_response(action, "completed")
    if surface is not None:
        pending.register(session_id, surface)
        envelope = awaiting_envelope(surface, "Waiting for outline approval.")
    else:
        if not persistent:
            _pending.clear(session_id)
        envelope = {**_drafting_envelope(session_id, result), "status": "completed"}
    envelope["action_response"] = response
    await end_turn(
        saver=None if persistent else saver, user_id=user_id, session_id=session_id, envelope=envelope,
        trace=result.get("trace"),
    )
    return envelope


@app.entrypoint
@guarded_entrypoint  # UUID session id + verified caller + ownership, before any session work
async def invoke(payload, context, session: AuthorizedSession):  # noqa: ANN001
    session_id = session.session_id
    user_id = session.user_id
    op = parse_op(payload)

    if op is RuntimeOp.JOB_STATUS:
        raise ValueError("job_status is not supported by the Drafting runtime")

    if op is RuntimeOp.RESUME:
        if payload.get("stream") is True:
            return StreamingResponse(
                _begin_stream_drafting_resume(payload, context, session_id, user_id),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
            )
        return await _resume(payload, context, session_id, user_id)

    prompt = payload.get("prompt", "")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")

    doc_type_raw = payload.get("document_type") or ""
    if not doc_type_raw:
        # ADR: free-text Drafting has no document_type | default to WHITEPAPER, the most general DocumentType.
        doc_type = DocumentType.WHITEPAPER
        payload_doc_type: str | None = None
    else:
        try:
            doc_type = DocumentType(doc_type_raw)
        except ValueError:
            raise ValueError(
                f"document_type must be one of: {', '.join(dt.value for dt in DocumentType)}"
            )
        payload_doc_type = doc_type_raw

    mode: str = payload.get("mode", "science")
    if mode not in ("science", "business"):
        mode = "science"

    export_formats_raw = payload.get("export_formats")
    payload_export_formats = (
        [f.lower() for f in export_formats_raw if isinstance(f, str) and f.lower() in _VALID_FORMATS]
        if isinstance(export_formats_raw, list)
        else []
    )
    stream = payload.get("stream") is True

    from drafting.graph import compile_graph

    # New run only: a resume (`_resume` above) must not re-recall long-term memory.
    with gateway_request_context(context, user_id, session_id):
        memories = await _drafting_long_term_context(user_id, prompt)

    saver, warm, persistent = await _drafting_checkpoint_context(user_id, session_id)
    # Cold thread (runtime restart / LRU eviction): rebuild short-term memory from ChatHistoryStore.
    # AgentCore Memory owns its own checkpoints, so a persistent saver restores no ChatHistoryStore
    # snapshot here (`begin_turn`'s `saver=None`); `warm` already reflects whether its checkpoint exists.
    history = await begin_turn(
        user_id=user_id,
        session_id=session_id,
        prompt=prompt,
        agent_type="drafting",
        document_type=doc_type.value,
        mode=mode,
        warm=warm,
        saver=None if persistent else saver,
    )
    if not persistent:
        _touch_thread(session_id)
    graph = compile_graph(checkpointer=saver)
    graph_config = {"configurable": {"thread_id": session_id, "actor_id": user_id}}
    # `warm` already distinguishes "existing checkpoint" (persistent or in-process) from "first
    # turn" for both branches: a persistent first turn has no checkpoint state, so it falls back to
    # the same cold `md`-export recovery as a plain in-process restart.
    carry_forward = (
        await _load_warm_carry_forward(graph, graph_config)
        if warm
        else await _load_cold_carry_forward(user_id, session_id)
    )

    initial_state = {
        # D16: history is dynamic content — it rides in messages, after the cached system prompt/tools
        "messages": [*history, HumanMessage(content=prompt)],
        "mode": mode,
        "long_term_context": memories,
        "user_id": user_id,
        "session_id": session_id,
        "document_type": doc_type,
        "payload_document_type": payload_doc_type,
        "payload_export_formats": payload_export_formats,
        "guidelines": [],
        "research_questions": [],
        "research_findings": [],
        "outline": "",
        "outline_revision": 0,
        "outline_approved": False,
        "outline_correction": None,
        "needs_outline_approval": False,
        "skip_outline_approval": False,
        "draft": "",
        "draft_document": None,
        "revision_feedback": None,
        "trace": [],
        "export_formats": [],
        "export_turn_id": "",
        "chart_specs": [],
        "chart_data": {},
        "chart_instructions": None,
        "exports": [],
        "stream": stream,
        **carry_forward,
    }

    log.info(f"Drafting agent invoked: session={session_id}, mode={mode}, doc_type={doc_type.value}")

    if stream:
        return StreamingResponse(
            _stream_drafting(graph, initial_state, graph_config, context, session, resume=False, prompt=prompt,
                             saver=saver, persistent=persistent),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    with gateway_request_context(context, user_id, session_id):
        result = await graph.ainvoke(initial_state, config=graph_config)

    surface = extract_interrupt_surface(result)
    if surface is not None:
        if not persistent:
            _pending.register(session_id, surface)
        envelope = awaiting_envelope(surface, "Waiting for outline approval.")
        await end_turn(saver=None if persistent else saver, user_id=user_id, session_id=session_id,
                       envelope=envelope, prompt=prompt)
        return envelope

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
        await end_turn(saver=None if persistent else saver, user_id=user_id, session_id=session_id,
                       envelope=response, prompt=prompt)
        return response

    trace_text = _format_trace(result.get("trace", []))
    full_output = text + trace_text
    exports = _public_exports(result)

    log.info(f"Drafting agent complete: session={session_id}, length={len(text)}, exports={len(exports)}")
    response = {
        "result": full_output,
        "text": text,
        "status": "completed",
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
    await end_turn(
        saver=None if persistent else saver, user_id=user_id, session_id=session_id, envelope=response,
        prompt=prompt, trace=result.get("trace"),
    )
    return response


async def _begin_stream_drafting_resume(payload, context, session_id: str, user_id: str):
    """Resolve the resume action, then stream the resumed run exactly like a fresh `op=run` (B1xx).

    Persistent-saver selection mirrors `_resume`/main.py's Research `_resume`: an actor-scoped
    config, `_restore_from_memory`'s expiry check in place of the ChatHistoryStore restore, and an
    ephemeral `pending` registry for the resolve step.
    """
    from drafting.graph import compile_graph

    events = ResearchEventStream()
    yield events.begin("drafting-final")
    try:
        persistent_saver = _persistent_drafting_checkpointer()
        persistent = persistent_saver is not None
        saver = persistent_saver if persistent else _checkpointer
        graph = compile_graph(checkpointer=saver)
        pending = PendingInterrupts() if persistent else _pending
        restore = _restore_from_memory if persistent else None
        kwargs: Dict[str, Any] = {}
        if restore is not None:
            kwargs["restore"] = restore

        action, terminal = await resolve_resume_action(
            payload, session_id=session_id, user_id=user_id, checkpointer=saver, graph=graph,
            pending=pending, is_cold=True if persistent else session_id not in _thread_ids, **kwargs,
        )
        if terminal is not None:
            message = "This request is no longer waiting for that input."
            response_body = terminal.get("action_response")
            if isinstance(response_body, dict) and response_body.get("message"):
                message = response_body["message"]
            yield events.emit("error", code=str(terminal.get("status", "error")), message=message)
            return

        await record_resume(user_id=user_id, session_id=session_id, action=action)
        if not persistent:
            _touch_thread(session_id)
        graph_config = {"configurable": {"thread_id": session_id, "actor_id": user_id}}
        async for frame in _stream_drafting(
            graph, Command(resume=resume_value(action)), graph_config, context, None, resume=True,
            session_id=session_id, user_id=user_id, events=events, saver=saver, persistent=persistent,
        ):
            yield frame
    except Exception as exc:  # noqa: BLE001
        log.error("Drafting resume stream failed (%s)", type(exc).__name__)
        yield events.emit("error", code="internal_error", message="The request could not be completed.")


async def _stream_drafting(
    graph,
    graph_input,
    graph_config: dict,
    context,
    session,
    *,
    resume: bool,
    session_id: str | None = None,
    user_id: str | None = None,
    events: ResearchEventStream | None = None,
    prompt: str | None = None,
    saver: Any = None,
    persistent: bool = False,
):
    """Stream drafting's node progress + live prose, then the short summary + Files surface (B1xx).

    `events.begin(...)` has already been emitted by the caller when resuming (the resume path needs
    to emit it before the replay/stale/expired checks); a fresh run emits it here instead. `saver`
    defaults to the module's in-process `_checkpointer` (existing test callers omit it); `persistent`
    selects the AgentCoreMemorySaver conventions (no local `_pending` registration, `end_turn`'s own
    background checkpoint snapshot skipped since AgentCore Memory already wrote it durably).
    """
    surface_id = "drafting-final"
    session_id = session_id or (session.session_id if session is not None else None)
    user_id = user_id or (session.user_id if session is not None else None)
    events = events or ResearchEventStream()
    saver = saver if saver is not None else _checkpointer
    if not resume:
        yield events.begin(surface_id)
    yield events.emit("progress", status="planning", step_label="Understanding the request")

    final_state: dict | None = None
    traces_sent = 0
    input_tokens = 0
    output_tokens = 0
    preview_started = False
    preview_text = ""
    try:
        with gateway_request_context(context, user_id, session_id):
            updates = graph.astream(graph_input, config=graph_config, stream_mode=["values", "updates", "custom"])
            async for has_value, update in with_heartbeats(updates):
                if not has_value:
                    yield b": heartbeat\n\n"
                    continue
                channel, payload = update
                if channel == "custom":
                    if preview_started and isinstance(payload, dict) and payload.get("type") == "prose_delta":
                        delta = payload.get("delta")
                        if isinstance(delta, str):
                            preview_text += delta
                            for chunk in text_chunks(delta):
                                yield events.emit("text_delta", component_id="stream-preview", field="content", delta=chunk)
                    continue
                if channel == "updates":
                    node_name = next(iter(payload), None) if isinstance(payload, dict) else None
                    if node_name is None:
                        continue
                    label = _NODE_PROGRESS_LABELS.get(node_name, node_name)
                    yield events.emit("progress", status="running", step_label=label)
                    if node_name == "route_request" and not preview_started:
                        node_update = payload.get("route_request") or {}
                        intent = node_update.get("intent") if isinstance(node_update, dict) else None
                        if intent in ("new_document", "revise", "approve_outline", "chat"):
                            yield events.emit("component_add", parent_id=None,
                                              component={"id": "root", "type": "Column", "children": []})
                            yield events.emit("component_add", parent_id="root", index=0,
                                              component={"id": "stream-preview", "type": "Markdown", "content": ""})
                            preview_started = True
                    continue
                # channel == "values": the full state snapshot after this super-step
                final_state = payload
                entries = payload.get("trace", [])
                for entry in entries[traces_sent:]:
                    yield events.trace(entry)
                    token_input = entry.get("input_tokens", 0) if isinstance(entry, dict) else getattr(entry, "input_tokens", 0)
                    token_output = entry.get("output_tokens", 0) if isinstance(entry, dict) else getattr(entry, "output_tokens", 0)
                    if isinstance(token_input, int) and token_input > 0:
                        input_tokens += token_input
                    if isinstance(token_output, int) and token_output > 0:
                        output_tokens += token_output
                traces_sent = len(entries)
        if final_state is None:
            raise RuntimeError("drafting graph returned no state")

        # B72: the outline-approval pause ends the stream here with `hitl_request`, not `surface_end`.
        state_snapshot = await graph.aget_state(graph_config)
        interrupts = list(getattr(state_snapshot, "interrupts", None) or [])
        if not interrupts:
            for task in getattr(state_snapshot, "tasks", None) or []:
                interrupts.extend(getattr(task, "interrupts", None) or [])
        pending_surface = extract_interrupt_surface({"__interrupt__": interrupts})
        if pending_surface is not None:
            if not persistent:
                _pending.register(session_id, pending_surface)
            yield events.emit("hitl_request", **pending_surface)
            await end_turn(
                saver=None if persistent else saver, user_id=user_id, session_id=session_id,
                envelope=awaiting_envelope(pending_surface, "Waiting for outline approval."), prompt=prompt,
            )
            return

        yield events.emit("usage", input_tokens=input_tokens, output_tokens=output_tokens,
                          total_tokens=input_tokens + output_tokens)
        text = final_ai_text(final_state.get("messages", [])) or "No response generated."
        exports = _public_exports(final_state)
        surface = build_drafting_surface(
            text, surface_id=surface_id, exports=exports, chart_specs=final_state.get("chart_specs", []),
            chart_instructions=final_state.get("chart_instructions"), document=final_state.get("draft_document"),
        ).model_dump(mode="json")
        if preview_started:
            surface = _stream_surface(surface)
        from shared.schemas.cei_ui import CEIUISurface
        CEIUISurface.model_validate(surface)

        response = {"result": text, "text": text, "ui": surface, "exports": exports}
        message_id = await end_turn(
            saver=None if persistent else saver, user_id=user_id, session_id=session_id, envelope=response,
            prompt=prompt, stream_trace_events=events.trace_events,
        )
        yield events.emit("progress", status="finalizing", step_label="Preparing the response")
        if preview_started:
            if preview_text:
                # ADR: the final surface never carries the full draft/outline text (B1xx #3) | clear it
                yield events.emit("component_update", component_id="stream-preview", patch={"content": ""})
            content = {**surface, "root_id": "drafting-content", "components": [
                component for component in surface["components"] if component["id"] not in ("root", "stream-preview")
            ]}
            root = next(component for component in surface["components"] if component["id"] == "root")
            for frame in events.components(content, parent_id="root", index=len(root["children"]) - 1):
                yield frame
        else:
            for frame in events.components(surface, parent_id=None, index=0):
                yield frame
        fields = {"surface_id": surface_id, "revision": 1, "root_id": "root" if preview_started else surface["root_id"], "final": True}
        if message_id:
            fields["message_id"] = message_id
        yield events.emit("surface_end", **fields)
    except Exception as exc:  # noqa: BLE001
        # Do not include exception strings; upstream errors may contain credentials.
        log.error("Drafting stream failed (%s)", type(exc).__name__)
        yield events.emit("error", code="internal_error", message="The request could not be completed.")


def _stream_surface(surface: dict) -> dict:
    """Wrap the final surface behind the live `stream-preview` component, matching research's pattern."""
    components = [dict(component) for component in surface["components"]]
    for component in components:
        if component["id"] == surface["root_id"]:
            component["id"] = "drafting-content"
            break
    components.append({"id": "stream-preview", "type": "Markdown", "content": ""})
    components.append({"id": "root", "type": "Column", "children": ["stream-preview", "drafting-content"]})
    return {**surface, "root_id": "root", "components": components}


if __name__ == "__main__":
    app.run(port=_runtime_port())
