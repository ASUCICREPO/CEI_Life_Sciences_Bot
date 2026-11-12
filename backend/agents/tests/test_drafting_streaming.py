"""B1xx: drafting_main's SSE event contract for op=run and op=resume streaming."""
from __future__ import annotations

import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

import drafting_main


def decode(frame):
    if isinstance(frame, bytes) and frame.startswith(b": heartbeat"):
        return None, "heartbeat", None
    fields = dict(line.split(": ", 1) for line in frame.decode().strip().splitlines())
    return int(fields["id"]), fields["event"], json.loads(fields["data"])


class _FakeGraph:
    def __init__(self, updates, final_values, interrupts=None):
        self._updates = updates
        self._final_values = final_values
        self._interrupts = interrupts or []

    async def astream(self, *_args, **_kwargs):
        for item in self._updates:
            yield item

    async def aget_state(self, _config):
        return SimpleNamespace(values=self._final_values, interrupts=self._interrupts, tasks=[])


@pytest.fixture(autouse=True)
def _no_gateway(monkeypatch):
    monkeypatch.setattr(drafting_main, "gateway_request_context", lambda *_a, **_k: nullcontext())


@pytest.fixture(autouse=True)
def _fake_end_turn(monkeypatch):
    async def _end_turn(**_kwargs):
        return "message-1"
    monkeypatch.setattr(drafting_main, "end_turn", _end_turn)


@pytest.mark.asyncio
async def test_stream_drafting_chat_intent_event_order():
    updates = [
        ("updates", {"route_request": {"intent": "chat"}}),
        ("custom", {"type": "prose_delta", "delta": "Hello "}),
        ("custom", {"type": "prose_delta", "delta": "world."}),
        ("updates", {"chat_answer": {}}),
        ("values", {
            "messages": [AIMessage(content="Hello world.")],
            "trace": [],
            "exports": [],
            "chart_specs": [],
            "chart_instructions": None,
            "draft_document": None,
        }),
    ]
    graph = _FakeGraph(updates, updates[-1][1])
    session = SimpleNamespace(user_id="user-a", session_id="session-a")

    frames = [
        decode(frame)
        async for frame in drafting_main._stream_drafting(graph, {}, {}, None, session, resume=False)
    ]
    kinds = [kind for _, kind, _ in frames]

    assert kinds[0] == "surface_begin"
    assert kinds[1] == "progress"
    assert "component_add" in kinds  # the stream-preview scaffold, added once route_request resolves
    assert kinds.index("component_add") > kinds.index("progress")
    assert "text_delta" in kinds
    assert kinds.index("text_delta") > kinds.index("component_add")
    assert "usage" in kinds
    assert kinds[-1] == "surface_end"
    assert kinds.count("error") == 0

    # the live preview is cleared once the (short) final answer has been delivered as text
    clears = [body for _, kind, body in frames if kind == "component_update" and body.get("component_id") == "stream-preview"]
    assert clears and clears[0]["patch"]["content"] == ""


@pytest.mark.asyncio
async def test_stream_drafting_export_intent_has_no_preview_scaffold():
    updates = [
        ("updates", {"route_request": {"intent": "export"}}),
        ("updates", {"export_documents": {}}),
        ("values", {
            "messages": [AIMessage(content="Document ready.")],
            "trace": [],
            "exports": [{"export_id": "6f1c2d3e-0000-4000-8000-000000000001_" + "a" * 32,
                         "format": "md", "filename": "report.md", "byte_size": 10}],
            "chart_specs": [],
            "chart_instructions": None,
            "draft_document": None,
        }),
    ]
    graph = _FakeGraph(updates, updates[-1][1])
    session = SimpleNamespace(user_id="user-a", session_id="session-a")

    frames = [
        decode(frame)
        async for frame in drafting_main._stream_drafting(graph, {}, {}, None, session, resume=False)
    ]
    kinds = [kind for _, kind, _ in frames]

    assert "text_delta" not in kinds
    assert kinds[-1] == "surface_end"
    export_components = [
        body["component"] for _, kind, body in frames
        if kind == "component_add" and body.get("component", {}).get("type") == "ExportFile"
    ]
    assert len(export_components) == 1
    assert export_components[0]["filename"] == "report.md"


@pytest.mark.asyncio
async def test_stream_drafting_outline_pause_emits_hitl_request():
    from drafting.nodes import _build_outline_approval_surface

    surface = _build_outline_approval_surface("- Intro\n- Methods", revision=1)
    updates = [
        ("updates", {"route_request": {"intent": "new_document"}}),
        ("values", {"outline": "- Intro\n- Methods", "trace": [], "messages": []}),
    ]
    graph = _FakeGraph(updates, updates[-1][1], interrupts=[SimpleNamespace(value=surface)])
    session = SimpleNamespace(user_id="user-a", session_id="session-a")

    frames = [
        decode(frame)
        async for frame in drafting_main._stream_drafting(graph, {}, {}, None, session, resume=False)
    ]
    kinds = [kind for _, kind, _ in frames]

    assert kinds[-1] == "hitl_request"
    assert "surface_end" not in kinds
    hitl_body = next(body for _, kind, body in frames if kind == "hitl_request")
    assert hitl_body["surface_id"] == "drafting-outline"
