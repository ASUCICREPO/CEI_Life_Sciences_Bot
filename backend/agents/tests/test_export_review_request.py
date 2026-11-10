from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

LAMBDAS = Path(__file__).resolve().parents[2] / "lambdas"
sys.path.insert(0, str(LAMBDAS / "shared"))
sys.path.insert(0, str(LAMBDAS / "exports-handler"))

from http_errors import ApiError
from review_request import prepare_reviewed_export
sys.path.remove(str(LAMBDAS / "exports-handler"))
sys.path.remove(str(LAMBDAS / "shared"))
from shared.persistence.chat_history import MessageRole
from shared.persistence.page import Page

SESSION = "6f1c2d3e-0000-4000-8000-000000000001"


def request(history, **changes):
    args = {"user_id": "alice", "session_id": SESSION, "turn_id": "t1",
            "body": {"format": "md", "reviewed": True}, "history": history}
    return prepare_reviewed_export(**(args | changes))


def turn(role=MessageRole.AI, user="alice", text="Persisted answer", status="completed"):
    return SimpleNamespace(message_id="t1", role=role, user_id=user, completion_status=status,
                           content_blocks=[SimpleNamespace(block_type="text", body=text)])


def test_reviewed_export_loads_persisted_turn_across_pages():
    history = Mock()
    cursor = {"session_id": SESSION, "sequence_number": 2}
    history.load_page.side_effect = [Page(items=[], next_key=cursor), Page(items=[turn()], next_key=None)]
    prepared = request(history)
    assert prepared.text == "Persisted answer"
    assert prepared.user_id == "alice" and prepared.format == "md"
    assert history.load_page.call_args.kwargs["start_key"] == cursor


@pytest.mark.parametrize("body", [{"format": "md"}, {"format": "md", "reviewed": False},
                                     {"format": "md", "reviewed": 1},
                                     {"format": "md", "reviewed": True, "text": "forged"},
                                     {"format": "pptx", "reviewed": True}])
def test_rejects_missing_review_extra_fields_and_unsupported_format(body):
    history = Mock()
    with pytest.raises(ApiError) as exc:
        request(history, body=body)
    assert exc.value.status == 400
    history.load_page.assert_not_called()


@pytest.mark.parametrize("saved_turn", [turn(role=MessageRole.HUMAN), turn(user="bob"), turn(text="")])
def test_rejects_human_foreign_and_empty_turns(saved_turn):
    history = Mock()
    history.load_page.return_value = Page(items=[saved_turn], next_key=None)
    with pytest.raises(ApiError) as exc:
        request(history)
    assert exc.value.status == 404


def test_requires_durable_completed_status():
    history = Mock()
    history.load_page.return_value = Page(items=[turn(status=None)], next_key=None)
    with pytest.raises(ApiError) as exc:
        request(history)
    assert exc.value.status == 404
