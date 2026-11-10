from __future__ import annotations

import importlib.util
import base64
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from shared.persistence.page import Page

LAMBDAS = Path(__file__).resolve().parents[2] / "lambdas"
sys.path.insert(0, str(LAMBDAS / "shared"))
spec = importlib.util.spec_from_file_location("export_handler", LAMBDAS / "exports-handler" / "handler.py")
handler = importlib.util.module_from_spec(spec)
spec.loader.exec_module(handler)
sys.path.remove(str(LAMBDAS / "shared"))

SESSION = "6f1c2d3e-0000-4000-8000-000000000001"
EXPORT = SESSION + "_" + "a" * 32
ITEM = {"user_id": "alice", "session_id": SESSION, "turn_id": "turn", "export_id": EXPORT,
        "s3_key": "secret-key", "filename": "report.pdf", "format": "pdf", "size": 1,
        "created_at": "2026-01-01T00:00:00Z", "status": "ready"}


def event(resource, user="alice", **params):
    return {"httpMethod": "GET", "resource": resource, "pathParameters": params,
            "requestContext": {"authorizer": {"claims": {"sub": user}}}}


def test_list_checks_ownership_and_never_presigns(monkeypatch):
    repo = Mock()
    repo.list_for_session.return_value = Page(items=[ITEM], next_key=None)
    monkeypatch.setattr(handler, "_exports", repo)
    monkeypatch.setattr(handler, "_metadata", SimpleNamespace(get=lambda user, session: {"session_id": session}))
    monkeypatch.setattr(handler, "_history", SimpleNamespace(owner_of=lambda session: "alice"))
    result = handler.handler(event("/sessions/{sessionId}/exports", sessionId=SESSION), None)
    assert result["statusCode"] == 200
    assert json.loads(result["body"])["items"][0]["export_id"] == EXPORT
    assert "secret-key" not in result["body"]
    repo.download_url.assert_not_called()
    result = handler.handler(event("/sessions/{sessionId}/exports", user="bob", sessionId=SESSION), None)
    assert result["statusCode"] == 404
    assert repo.list_for_session.call_count == 1


def test_download_is_owner_scoped_and_checks_session(monkeypatch):
    repo = Mock()
    repo.get.side_effect = lambda user, key: ITEM if user == "alice" and key == EXPORT else None
    repo.download_url.return_value = "https://example.org/signed"
    monkeypatch.setattr(handler, "_exports", repo)
    monkeypatch.setattr(handler, "_metadata", SimpleNamespace(get=lambda user, session: {"session_id": session}))
    monkeypatch.setattr(handler, "_history", SimpleNamespace(owner_of=lambda session: "alice"))
    result = handler.handler(event("/exports/{exportId}/download", user="bob", exportId=EXPORT), None)
    assert result["statusCode"] == 404
    repo.download_url.assert_not_called()
    result = handler.handler(event("/exports/{exportId}/download", exportId=EXPORT), None)
    assert result["statusCode"] == 200
    assert result["headers"]["Cache-Control"] == "no-store"
    assert json.loads(result["body"])["download_url"] == "https://example.org/signed"
    repo.download_url.assert_called_once_with(ITEM)


def test_list_rejects_foreign_cursor_and_missing_session(monkeypatch):
    repo = Mock()
    monkeypatch.setattr(handler, "_exports", repo)
    monkeypatch.setattr(handler, "_metadata", SimpleNamespace(get=lambda user, session: {"session_id": session}))
    monkeypatch.setattr(handler, "_history", SimpleNamespace(owner_of=lambda session: "alice"))
    request = event("/sessions/{sessionId}/exports", sessionId=SESSION)
    foreign = {"user_id": "bob", "export_id": EXPORT}
    request["queryStringParameters"] = {"cursor": base64.urlsafe_b64encode(json.dumps(foreign).encode()).decode()}
    assert handler.handler(request, None)["statusCode"] == 400
    repo.list_for_session.assert_not_called()
    monkeypatch.setattr(handler, "_metadata", SimpleNamespace(get=lambda user, session: None))
    assert handler.handler(event("/sessions/{sessionId}/exports", sessionId=SESSION), None)["statusCode"] == 404
