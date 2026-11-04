"""drafting_main passes its validated document_type into begin_turn (turn context for session resume)."""
from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage

import drafting_main
from drafting.state import DocumentType


class _Context:
    session_id = "drafting-runtime-session"
    request_headers = {"Authorization": "Bearer test-token:test-user"}


class _CompletedGraph:
    async def ainvoke(self, state, config):
        return {"messages": [AIMessage(content="Draft.")], "trace": [], "exports": []}


@pytest.fixture
def calls(monkeypatch) -> list[dict]:
    recorded: list[dict] = []

    async def _begin_turn(**kwargs):
        recorded.append(kwargs)
        return []

    async def _end_turn(**_):
        return None

    import drafting.graph as graph_module

    monkeypatch.setattr(graph_module, "compile_graph", lambda **_: _CompletedGraph())
    monkeypatch.setattr(drafting_main, "begin_turn", _begin_turn)
    monkeypatch.setattr(drafting_main, "end_turn", _end_turn)
    return recorded


@pytest.mark.parametrize("doc_type", list(DocumentType))
def test_begin_turn_receives_validated_document_type(calls, doc_type) -> None:
    response = asyncio.run(drafting_main.invoke({"prompt": "Write it", "document_type": doc_type.value}, _Context()))
    assert response["text"] == "Draft."
    assert len(calls) == 1
    assert calls[0]["agent_type"] == "drafting"
    assert calls[0]["document_type"] == doc_type.value
    assert type(calls[0]["document_type"]) is str


@pytest.mark.parametrize("raw", ["bogus", "", None])
def test_invalid_document_type_still_raises_before_begin_turn(calls, raw) -> None:
    payload = {"prompt": "Write it"}
    if raw is not None:
        payload["document_type"] = raw
    with pytest.raises(ValueError, match="document_type must be one of"):
        asyncio.run(drafting_main.invoke(payload, _Context()))
    assert calls == []
