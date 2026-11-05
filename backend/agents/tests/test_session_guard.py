"""Session guard: UUID session id -> verified caller -> ownership, before the wrapped handler ever runs."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest

import shared.runtime.history as history
from shared.persistence.chat_metadata import SessionNotFoundError
from shared.persistence.ownership import GENERIC_INTERNAL_ERROR, OwnershipCheckError
from shared.runtime import session_guard
from shared.runtime.session_guard import AuthorizedSession, guarded_entrypoint
from tests.conftest import auth_headers

SESSION = "0b0c4f7e-4444-4a44-8a44-0000000000aa"


class _Context:
    def __init__(self, session_id=None, headers=None) -> None:
        if session_id is not None:
            self.session_id = session_id
        self.request_headers = headers if headers is not None else auth_headers("alice")


class _Recorder:
    """Records assert_owner calls into a shared event log; optionally raises."""

    def __init__(self, events: list, exc: Exception | None = None) -> None:
        self.events = events
        self.exc = exc
        self.calls: list[dict] = []

    async def assert_owner(self, *, user_id: str, session_id: str) -> None:
        self.calls.append({"user_id": user_id, "session_id": session_id})
        self.events.append("assert_owner")
        if self.exc is not None:
            raise self.exc


@pytest.fixture
def events():
    return []


def _install(monkeypatch, events, exc=None) -> _Recorder:
    recorder = _Recorder(events, exc)
    monkeypatch.setattr(history, "get_recorder", lambda: recorder)
    return recorder


def _guarded(events):
    calls: list[tuple] = []

    @guarded_entrypoint
    async def handler(payload, context, session):
        events.append("handler")
        calls.append((payload, context, session))
        return {"status": "ok"}

    return handler, calls


def _body(response) -> dict:
    return json.loads(response.body)


@pytest.mark.parametrize("session_id", [None, "default", "session-1", "", "{" + SESSION + "}"])
def test_non_uuid_session_id_is_400_and_handler_not_called(monkeypatch, events, session_id):
    recorder = _install(monkeypatch, events)
    handler, calls = _guarded(events)
    response = asyncio.run(handler({}, _Context(session_id)))
    assert response.status_code == 400
    assert _body(response) == {"status": "error", "message": "A valid session id is required."}
    assert calls == []
    assert recorder.calls == []  # no ownership lookup for an invalid id


def test_uuid_with_trailing_newline_is_400(monkeypatch, events):
    recorder = _install(monkeypatch, events)
    handler, calls = _guarded(events)
    response = asyncio.run(handler({}, _Context(SESSION + "\n")))
    assert getattr(response, "status_code", None) == 400
    assert calls == [] and recorder.calls == []


def test_unresolvable_user_is_401_and_handler_not_called(monkeypatch, events):
    recorder = _install(monkeypatch, events)
    handler, calls = _guarded(events)
    # payload user_id must not be trusted as identity
    response = asyncio.run(handler({"user_id": "alice"}, _Context(SESSION, headers={})))
    assert response.status_code == 401
    assert _body(response)["message"] == "Authentication is required."
    assert calls == []
    assert recorder.calls == []


def test_invalid_bearer_token_is_401(monkeypatch, events):
    _install(monkeypatch, events)
    handler, calls = _guarded(events)
    response = asyncio.run(handler({}, _Context(SESSION, headers={"Authorization": "Bearer forged"})))
    assert response.status_code == 401
    assert calls == []


def test_foreign_session_is_404_and_handler_not_called(monkeypatch, events):
    recorder = _install(monkeypatch, events, SessionNotFoundError(SESSION))
    handler, calls = _guarded(events)
    response = asyncio.run(handler({}, _Context(SESSION)))
    assert response.status_code == 404
    assert _body(response) == {"status": "error", "message": "Session not found."}
    assert recorder.calls == [{"user_id": "alice", "session_id": SESSION}]
    assert calls == []
    assert events == ["assert_owner"]


def test_ownership_check_error_is_generic_500_and_logged(monkeypatch, events):
    _install(monkeypatch, events, OwnershipCheckError("secret-cause"))
    fake_log = MagicMock()
    monkeypatch.setattr(session_guard, "log", fake_log)
    handler, calls = _guarded(events)
    response = asyncio.run(handler({}, _Context(SESSION)))
    assert response.status_code == 500
    assert _body(response) == {"status": "error", "message": GENERIC_INTERNAL_ERROR}
    assert "secret-cause" not in response.body.decode()
    fake_log.exception.assert_called_once()
    assert calls == []


def test_success_calls_handler_once_with_authorized_session_after_ownership(monkeypatch, events):
    recorder = _install(monkeypatch, events)
    handler, calls = _guarded(events)
    payload = {"prompt": "hi"}
    context = _Context(SESSION)
    result = asyncio.run(handler(payload, context))
    assert result == {"status": "ok"}
    assert len(calls) == 1
    got_payload, got_context, got_session = calls[0]
    assert got_payload is payload and got_context is context
    assert got_session == AuthorizedSession(user_id="alice", session_id=SESSION)
    assert recorder.calls == [{"user_id": "alice", "session_id": SESSION}]
    assert events == ["assert_owner", "handler"]  # ownership proven before the handler runs


def test_payload_user_id_is_ignored_in_favor_of_token_sub(monkeypatch, events):
    recorder = _install(monkeypatch, events)
    handler, calls = _guarded(events)
    asyncio.run(handler({"user_id": "mallory"}, _Context(SESSION)))
    assert recorder.calls[0]["user_id"] == "alice"
    assert calls[0][2].user_id == "alice"
