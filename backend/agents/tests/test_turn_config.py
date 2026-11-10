import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from shared.config import config
from shared.persistence.chat_metadata import ChatMetadataRepository
from shared.runtime.model_policy import ModelPolicyError
from shared.runtime.turn_config import resolve_turn_config


def test_existing_session_settings_override_payload():
    recorder = SimpleNamespace(session_metadata=AsyncMock(return_value=SimpleNamespace(mode="business", model_id=config.default_model_id)))
    result = asyncio.run(resolve_turn_config(recorder, user_id="owner", session_id="session", initial_mode="science"))
    assert result.mode == "business" and result.model_id == config.default_model_id
    recorder.session_metadata.assert_awaited_once_with(user_id="owner", session_id="session")


def test_unverified_saved_model_is_rejected():
    recorder = SimpleNamespace(session_metadata=AsyncMock(return_value=SimpleNamespace(mode="science", model_id="other-model")))
    with pytest.raises(ModelPolicyError):
        asyncio.run(resolve_turn_config(recorder, user_id="owner", session_id="session", initial_mode="science"))


def test_legacy_settings_are_initialized_and_race_winner_is_rechecked():
    initial = SimpleNamespace(mode=None, model_id=None)
    concurrent = SimpleNamespace(mode="science", model_id=None)
    final = SimpleNamespace(mode="science", model_id=config.default_model_id)
    recorder = SimpleNamespace(session_metadata=AsyncMock(return_value=initial),
                               initialize_turn_settings=AsyncMock(side_effect=[concurrent, final]))
    result = asyncio.run(resolve_turn_config(recorder, user_id="owner", session_id="session", initial_mode="business"))
    assert result.mode == "science" and result.model_id == config.default_model_id
    assert recorder.initialize_turn_settings.await_args_list[0].kwargs["mode"] == "business"
    assert recorder.initialize_turn_settings.await_args_list[1].kwargs["mode"] is None


def test_missing_and_invalid_initial_mode():
    recorder = SimpleNamespace(session_metadata=AsyncMock(return_value=None))
    result = asyncio.run(resolve_turn_config(recorder, user_id="owner", session_id="new", initial_mode=None))
    assert result.mode == "science"
    with pytest.raises(ModelPolicyError):
        asyncio.run(resolve_turn_config(recorder, user_id="owner", session_id="new", initial_mode="invalid"))


def test_metadata_lookup_is_strongly_consistent():
    repository = object.__new__(ChatMetadataRepository)
    repository._table = Mock()
    repository._table.get_item.return_value = {}
    assert repository.get("owner", "session") is None
    repository._table.get_item.assert_called_once_with(Key={"user_id": "owner", "session_id": "session"}, ConsistentRead=True)


def test_research_turn_uses_saved_mode_and_model(monkeypatch):
    import main
    from langchain_core.messages import AIMessage
    from shared.runtime import history

    recorder = history.get_recorder()
    monkeypatch.setattr(recorder, "session_metadata", AsyncMock(return_value=SimpleNamespace(
        mode="business", model_id=config.default_model_id)))
    monkeypatch.setattr(history, "get_recorder", lambda: recorder)
    seen = {}

    class Graph:
        async def ainvoke(self, initial_state, config):
            seen.update(initial_state)
            return {"messages": [AIMessage(content="done")], "trace": [], "retrieved_results": []}

    monkeypatch.setattr("research.graph.compile_graph", lambda **_: Graph())
    context = SimpleNamespace(session_id="0b0c4f7e-3333-4a33-8a33-000000000001",
                              request_headers={"Authorization": "Bearer test-token:test-user"})
    response = asyncio.run(main.invoke({"prompt": "question", "mode": "science"}, context))
    assert response["text"] == "done"
    assert seen["mode"] == "business" and seen["model_id"] == config.default_model_id


def test_research_rejects_unverified_model_before_history_write(monkeypatch):
    import main
    from shared.runtime import history

    recorder = history.get_recorder()
    monkeypatch.setattr(recorder, "session_metadata", AsyncMock(return_value=SimpleNamespace(
        mode="science", model_id="unverified-model")))
    start_turn = AsyncMock()
    monkeypatch.setattr(recorder, "start_turn", start_turn)
    monkeypatch.setattr(history, "get_recorder", lambda: recorder)
    context = SimpleNamespace(session_id="0b0c4f7e-3333-4a33-8a33-000000000001",
                              request_headers={"Authorization": "Bearer test-token:test-user"})
    response = asyncio.run(main.invoke({"prompt": "question"}, context))
    assert response.status_code == 400
    assert b"unverified-model" not in response.body
    start_turn.assert_not_awaited()


def test_stream_reports_racing_model_change_without_leaking_id(monkeypatch):
    import main
    from shared.runtime import history

    recorder = history.get_recorder()
    monkeypatch.setattr(recorder, "session_metadata", AsyncMock(return_value=SimpleNamespace(
        mode="science", model_id="private-model")))
    monkeypatch.setattr(history, "get_recorder", lambda: recorder)
    context = SimpleNamespace(session_id="0b0c4f7e-3333-4a33-8a33-000000000001",
                              request_headers={"Authorization": "Bearer test-token:test-user"})

    async def invoke_and_read():
        response = await main.invoke({"prompt": "question", "stream": True}, context)
        return b"".join([chunk async for chunk in response.body_iterator])

    output = asyncio.run(invoke_and_read())
    assert b"model_unavailable" in output and b"private-model" not in output


def test_streaming_research_uses_saved_settings(monkeypatch):
    import main
    from shared.runtime import history

    recorder = history.get_recorder()
    monkeypatch.setattr(recorder, "session_metadata", AsyncMock(return_value=SimpleNamespace(
        mode="business", model_id=config.default_model_id)))
    monkeypatch.setattr(history, "get_recorder", lambda: recorder)
    seen = []

    async def stream(_context, _session, _prompt, mode, model_id):
        seen.append((mode, model_id))
        yield b"data: test\n\n"

    monkeypatch.setattr(main, "_begin_stream_research", stream)
    context = SimpleNamespace(session_id="0b0c4f7e-3333-4a33-8a33-000000000001",
                              request_headers={"Authorization": "Bearer test-token:test-user"})

    async def invoke_and_read():
        response = await main.invoke({"prompt": "question", "mode": "science", "stream": True}, context)
        return [chunk async for chunk in response.body_iterator]

    assert asyncio.run(invoke_and_read()) == [b"data: test\n\n"]
    assert seen == [("business", config.default_model_id)]
