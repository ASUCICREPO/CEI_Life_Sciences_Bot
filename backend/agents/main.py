"""AgentCore Runtime entry point for the CEI Life Sciences Research Agent."""
from __future__ import annotations

import logging
import os
import re
import json
from collections import OrderedDict
from functools import lru_cache

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from starlette.responses import StreamingResponse
from shared.presenters import build_research_surface
from shared.response_text import final_ai_text
from shared.runtime.history import begin_turn, end_turn
from shared.runtime.app_factory import create_app
from shared.streaming.trace_redaction import redacts_trace_input
from shared.runtime.session_guard import AuthorizedSession, guarded_entrypoint
from shared.tools.gateway_context import gateway_request_context
from shared.streaming.research import ResearchEventStream, text_chunks, with_heartbeats
from typing import List, Dict, Union, Any

app = create_app()  # CORS for direct browser calls when CEI_ALLOWED_ORIGINS is set
log = app.logger
logging.basicConfig(level=logging.INFO)

_CHECKPOINT_LIMIT = 128
_checkpointer = InMemorySaver()
_thread_ids: OrderedDict[str, bool] = OrderedDict()


@lru_cache(maxsize=1)
def _persistent_research_checkpointer():
    """Opt in only with a configured AgentCore Memory ID; local demo keeps its saver."""
    memory_id = os.environ.get("CEI_RESEARCH_MEMORY_ID", "").strip()
    if not memory_id:
        return None
    from shared.memory.agentcore import create_checkpointer

    return create_checkpointer(memory_id)


async def _research_checkpoint_context(user_id: str, session_id: str):
    """Choose the saver and detect an existing actor-scoped persistent checkpoint.

    A first turn after migration rehydrates chat history; existing persistent
    checkpoints already contain the conversation and must not be rehydrated twice.
    Memory failures propagate instead of silently falling back across sessions.
    """
    persistent = _persistent_research_checkpointer()
    if persistent is None:
        return _checkpointer, session_id in _thread_ids, False
    config = {"configurable": {"thread_id": session_id, "actor_id": user_id}}
    checkpoint = await persistent.aget_tuple(config)
    return persistent, checkpoint is not None, True


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


@redacts_trace_input  # B7: model ids / guardrails / prompts / secrets never reach trace_text
def _format_trace(trace_entries: List[Dict[str, Any]]) -> str:
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
@guarded_entrypoint  # UUID session id + verified caller + ownership, before any session work
async def invoke(payload, context, session: AuthorizedSession):  # noqa: ANN001
    prompt = payload.get("prompt", "")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")

    session_id = session.session_id
    user_id = session.user_id
    mode: str = payload.get("mode", "science")

    if mode not in ("science", "business"):
        mode = "science"

    from research.graph import compile_graph

    if payload.get("stream") is True:
        return StreamingResponse(
            _begin_stream_research(context, session, prompt, mode),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    saver, warm, persistent = await _research_checkpoint_context(user_id, session_id)
    # Legacy in-memory mode restores snapshots; AgentCore Memory owns its own checkpoints.
    history = await begin_turn(
        user_id=user_id,
        session_id=session_id,
        prompt=prompt,
        agent_type="research",
        mode=mode,
        warm=warm,
        saver=None if persistent else saver,
    )
    if not persistent:
        _touch_thread(session_id)
    graph = compile_graph(checkpointer=saver)

    initial_state = {
        # D16: history is dynamic content — it rides in messages, after the cached system prompt/tools
        "messages": [*history, HumanMessage(content=prompt)],
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

    log.info(f"Research agent invoked: session={session_id}, mode={mode}")

    graph_config = {"configurable": {"thread_id": session_id, "actor_id": user_id}}
    with gateway_request_context(context, user_id, session_id):
        result = await graph.ainvoke(initial_state, config=graph_config)

    text = final_ai_text(result.get("messages", []))
    retrieved = result.get("retrieved_results", [])
    if text is None:
        text = "No response generated."
        response = {
            "result": text,
            "text": text,
            "ui": build_research_surface(
                text,
                surface_id="research-final",
                retrieved_results=retrieved,
                document=result.get("response_document"),
            ).model_dump(mode="json"),
        }
        await end_turn(saver=None if persistent else saver, user_id=user_id, session_id=session_id, envelope=response, prompt=prompt)
        return response

    trace_text = _format_trace(result.get("trace", []))
    full_output = text + trace_text

    log.info(f"Research agent complete: session={session_id}, length={len(text)}")
    response = {
        "result": full_output,
        "text": text,
        "ui": build_research_surface(
            text,
            surface_id="research-final",
            retrieved_results=retrieved,
            document=result.get("response_document"),
        ).model_dump(mode="json"),
    }
    # stores `text` (not `result`): the trace is diagnostic output, not conversation content
    await end_turn(
        saver=None if persistent else saver,
        user_id=user_id, session_id=session_id, envelope=response, prompt=prompt, retrieved_results=retrieved,
        trace=result.get("trace"),
    )
    return response


async def _begin_stream_research(context, session, prompt, mode):
    """Create async storage clients on the same loop that consumes the response."""
    from research.graph import compile_graph

    try:
        # Verify the forwarded token before making a history mutation.
        with gateway_request_context(context, session.user_id, session.session_id):
            saver, warm, persistent = await _research_checkpoint_context(session.user_id, session.session_id)
            history = await begin_turn(user_id=session.user_id, session_id=session.session_id, prompt=prompt,
                                       agent_type="research", mode=mode, warm=warm,
                                       saver=None if persistent else saver)
            if not persistent:
                _touch_thread(session.session_id)
            graph = compile_graph(checkpointer=saver)
            initial = {"messages": [*history, HumanMessage(content=prompt)], "mode": mode,
                       "user_id": session.user_id, "session_id": session.session_id, "query": prompt,
                       "triage_plan": None, "retrieved_results": [], "synthesis": "",
                       "response_document": None, "trace": [], "stream": True}
            config = {"configurable": {"thread_id": session.session_id, "actor_id": session.user_id}}
            async for frame in _stream_research(graph, initial, config, context, session, prompt, saver, persistent):
                yield frame
    except Exception as exc:
        log.error("Research stream initialization failed (%s)", type(exc).__name__)
        events = ResearchEventStream()
        yield events.begin("research-final")
        yield events.emit("error", code="internal_error", message="The request could not be completed.")


async def _stream_research(graph, initial_state, graph_config, context, session, prompt, saver, persistent):
    """Stream model-produced prose and graph progress before the grounded surface."""
    events = ResearchEventStream()
    surface_id = "research-final"
    yield events.begin(surface_id)
    yield events.emit("progress", status="planning", step_label="Planning research")
    final_state = None
    traces_sent = 0
    preview_started = False
    preview_text = ""
    pending_components = []
    groundables_sent = False
    try:
        with gateway_request_context(context, session.user_id, session.session_id):
            updates = graph.astream(initial_state, config=graph_config, stream_mode=["values", "custom"])
            async for has_value, update in with_heartbeats(updates):
                if not has_value:
                    yield b": heartbeat\n\n"
                    continue
                channel, value = update
                if channel == "custom":
                    if preview_started and isinstance(value, dict) and value.get("type") == "prose_delta":
                        delta = value.get("delta")
                        if isinstance(delta, str):
                            preview_text += delta
                            for chunk in text_chunks(delta):
                                yield events.emit("text_delta", component_id="stream-preview", field="content", delta=chunk)
                    continue
                final_state = value
                entries = value.get("trace", [])
                for entry in entries[traces_sent:]:
                    yield events.trace(entry)
                traces_sent = len(entries)
                if not preview_started and value.get("triage_plan"):
                    from research.nodes import estimate_research_duration_seconds
                    if estimate_research_duration_seconds(value["triage_plan"]) > 600:
                        yield events.emit("error", code="service_unavailable",
                                          message="This request needs background processing, which is not available yet. Try a narrower question.")
                        return
                    yield events.emit("component_add", parent_id=None,
                                      component={"id": "root", "type": "Column", "children": []})
                    yield events.emit("component_add", parent_id="root", index=0,
                                      component={"id": "stream-preview", "type": "Markdown", "content": ""})
                    preview_started = True
                if value.get("grounding_pending") and not groundables_sent:
                    # The structured document is valid, but its numeric data has not
                    # yet passed ground_synthesis. Publish pending before resuming it.
                    provisional = build_research_surface(
                        value.get("synthesis", ""), surface_id=surface_id,
                        retrieved_results=value.get("retrieved_results", []),
                        document=value.get("response_document"),
                    ).model_dump(mode="json")
                    for component in provisional["components"]:
                        if component["type"] not in ("Table", "StatGrid", "Graph", "Artifact"):
                            continue
                        if component["type"] == "Artifact" and component.get("data") is None:
                            continue
                        provisional_id = f"stream-ground-{len(pending_components)}"
                        pending_components.append((provisional_id, component))
                        yield events.emit("component_add", parent_id="root",
                                          component={**component, "id": provisional_id})
                        yield events.emit("grounding_update", component_id=provisional_id, status="pending")
                    groundables_sent = True
                # Emit status only after real node updates, not synthetic output chunks.
                if value.get("synthesis"):
                    yield events.emit("progress", status="verifying", step_label="Preparing the answer")
                elif value.get("triage_plan"):
                    yield events.emit("progress", status="running", step_label="Searching sources")
        if final_state is None:
            raise RuntimeError("research graph returned no state")
        text = final_ai_text(final_state.get("messages", [])) or "No response generated."
        retrieved = final_state.get("retrieved_results", [])
        surface = build_research_surface(text, surface_id=surface_id, retrieved_results=retrieved,
                                         document=final_state.get("response_document")).model_dump(mode="json")
        surface, preview_final, grounding = _stream_surface(
            surface, final_state.get("response_document"), preview_text, pending_components)
        # Rewriting the presenter graph must preserve the complete 1.1 contract,
        # including component/depth limits and all child references, before it is
        # persisted or exposed as the final surface.
        from shared.schemas.cei_ui import CEIUISurface
        CEIUISurface.model_validate(surface)
        # History accepts the 1.1-compatible graph; the streamed transport is 1.2.
        response = {"result": text, "text": text, "ui": surface}
        message_id = await end_turn(saver=None if persistent else saver, user_id=session.user_id,
                                    session_id=session.session_id, envelope=response, prompt=prompt,
                                    retrieved_results=retrieved, stream_trace_events=events.trace_events)
        yield events.emit("progress", status="finalizing", step_label="Preparing the response")
        if preview_final != preview_text:
            yield events.emit("component_update", component_id="stream-preview", patch={"content": preview_final})
        for provisional_id, component, status in grounding:
            if component is not None:
                yield events.emit("component_update", component_id=provisional_id,
                                  patch={key: value for key, value in component.items() if key not in ("id", "type")})
            yield events.emit("grounding_update", component_id=provisional_id, status=status)
        content = {**surface, "root_id": "research-content", "components": [
            component for component in surface["components"]
            if component["id"] not in ("root", "stream-preview") and not component["id"].startswith("stream-ground-")
        ]}
        # The preview and surviving pending blocks have already been appended to
        # root. Insert the final content after them, matching the stored graph.
        root = next(component for component in surface["components"] if component["id"] == "root")
        for frame in events.components(content, parent_id="root", index=len(root["children"]) - 1):
            yield frame
        fields = {"surface_id": surface_id, "revision": 1, "root_id": "root", "final": True}
        if message_id:
            fields["message_id"] = message_id
        yield events.emit("surface_end", **fields)
    except Exception as exc:
        # Do not include exception strings; upstream errors may contain credentials.
        log.error("Research stream failed (%s)", type(exc).__name__)
        yield events.emit("error", code="internal_error", message="The request could not be completed.")


def _groundable_signature(component):
    kind = component.get("type")
    if kind == "Table":
        values = [component.get("columns"), component.get("rows")]
    elif kind == "StatGrid":
        values = [[{key: value for key, value in metric.items() if key != "verified"}
                   for metric in component.get("metrics", [])]]
    elif kind == "Graph":
        values = [component.get("graph_type"), component.get("categories"), component.get("series")]
    else:
        values = [component.get("html"), component.get("data")]
    return kind, json.dumps(values, sort_keys=True, ensure_ascii=False, default=str)


def _grounding_status(component, document):
    if component["type"] == "Table":
        return "partially_verified" if component.get("flagged_cells") else "verified"
    if component["type"] == "StatGrid":
        verified = [metric.get("verified", True) for metric in component.get("metrics", [])]
        return "verified" if all(verified) else "partially_verified" if any(verified) else "unverified"
    match = re.match(r"^s(\d+)-b(\d+)", component["id"])
    if match and document is not None:
        try:
            block = document.sections[int(match[1])].blocks[int(match[2])]
            return getattr(block, "verification", None) or "unverified"
        except (IndexError, AttributeError):
            pass
    return "unverified"


def _stream_surface(surface, document, preview_text, pending_components=()):
    """Keep live prose in the final graph without duplicating its structured block."""
    from shared.schemas.response_blocks import ProseBlock

    components = [dict(component) for component in surface["components"]]
    for component in components:
        if component["id"] == surface["root_id"]:
            component["id"] = "research-content"
            break
    final_preview = ""
    if preview_text and document is not None:
        blocks = (block for section in document.sections for block in section.blocks)
        first_prose = next((block.markdown for block in blocks if isinstance(block, ProseBlock)), None)
        # Only remove the matching deterministic presenter component; otherwise
        # clear the preview and keep the complete final surface unchanged.
        match = next((item for item in components if item.get("type") == "Markdown"
                      and item.get("id", "").endswith("-prose") and item.get("content") == first_prose), None)
        if match is not None:
            final_preview = first_prose
            removed_id = match["id"]
            components.remove(match)
            for item in components:
                if isinstance(item.get("children"), list):
                    item["children"] = [child for child in item["children"] if child != removed_id]
    grounding = []
    moved = []
    for provisional_id, provisional in pending_components:
        match = next((item for item in components if _groundable_signature(item) == _groundable_signature(provisional)), None)
        if match is None:
            grounding.append((provisional_id, None, "dropped"))
            continue
        status = _grounding_status(match, document)
        original_id = match["id"]
        components.remove(match)
        for item in components:
            if isinstance(item.get("children"), list):
                item["children"] = [child for child in item["children"] if child != original_id]
        moved.append({**match, "id": provisional_id})
        grounding.append((provisional_id, match, status))
    components.extend([
        {"id": "stream-preview", "type": "Markdown", "content": final_preview},
        *moved,
        {"id": "root", "type": "Column", "children": ["stream-preview", *[item["id"] for item in moved], "research-content"]},
    ])
    return {**surface, "root_id": "root", "components": components}, final_preview, grounding


if __name__ == "__main__":
    app.run(port=_runtime_port())
