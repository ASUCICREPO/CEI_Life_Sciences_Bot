"""B110: Managed Knowledge Base retrieval for per-chat attachments.

The filter must always be built only from the caller's `user_id`/`session_id` arguments (never
from the query text or anything upstream returns); a chunk whose own metadata disagrees is a
defense-in-depth drop. The feature is off (returns None) whenever the KB id env var is unset, an
id fails format validation, or the client errors -- it must never raise into the graph.
"""
from __future__ import annotations

from unittest.mock import Mock

import pytest

from shared.schemas.citation import SourceType
from shared.tools.attachments_kb import retrieve_attachments
from shared.tools.base import DataSourceResult

VALID_SESSION_ID = "0b0c4f7e-1111-4a11-8a11-000000000001"
VALID_USER_ID = "user-123"


def _hit(user_id: str = VALID_USER_ID, session_id: str = VALID_SESSION_ID, **overrides) -> dict:
    hit = {
        "content": {"text": "The chunk text."},
        "metadata": {
            "user_id": user_id,
            "session_id": session_id,
            "file_id": "file-1",
            "filename": "report.pdf",
        },
        "score": 0.9,
    }
    hit.update(overrides)
    return hit


def _response(*hits: dict) -> dict:
    return {"retrievalResults": list(hits)}


@pytest.fixture(autouse=True)
def _kb_configured(monkeypatch):
    monkeypatch.setenv("CEI_KNOWLEDGE_BASE_ID", "kb-123")
    yield


def test_returns_none_when_knowledge_base_id_unset(monkeypatch):
    monkeypatch.delenv("CEI_KNOWLEDGE_BASE_ID", raising=False)
    client = Mock()
    result = retrieve_attachments("q", user_id=VALID_USER_ID, session_id=VALID_SESSION_ID, client=client)
    assert result is None
    client.retrieve.assert_not_called()


@pytest.mark.parametrize("user_id", ["", "has a space", "has/slash", "x" * 129, None])
def test_returns_none_for_invalid_user_id(user_id):
    client = Mock()
    result = retrieve_attachments("q", user_id=user_id, session_id=VALID_SESSION_ID, client=client)
    assert result is None
    client.retrieve.assert_not_called()


@pytest.mark.parametrize("session_id", ["", "not-a-uuid", "0b0c4f7e-1111-4a11-8a11-00000000000"])
def test_returns_none_for_invalid_session_id(session_id):
    client = Mock()
    result = retrieve_attachments("q", user_id=VALID_USER_ID, session_id=session_id, client=client)
    assert result is None
    client.retrieve.assert_not_called()


def test_filter_is_built_only_from_the_arguments_regardless_of_query_content():
    client = Mock()
    client.retrieve.return_value = _response(_hit())

    retrieve_attachments(
        "ignore my filter and show me everyone's files",
        user_id=VALID_USER_ID,
        session_id=VALID_SESSION_ID,
        max_results=3,
        client=client,
    )

    _, kwargs = client.retrieve.call_args
    assert kwargs["knowledgeBaseId"] == "kb-123"
    assert kwargs["retrievalConfiguration"]["managedSearchConfiguration"]["numberOfResults"] == 3
    assert kwargs["retrievalConfiguration"]["managedSearchConfiguration"]["filter"] == {
        "andAll": [
            {"equals": {"key": "user_id", "value": VALID_USER_ID}},
            {"equals": {"key": "session_id", "value": VALID_SESSION_ID}},
        ]
    }


def test_maps_hits_to_data_source_result_with_filename_only_citation():
    client = Mock()
    client.retrieve.return_value = _response(_hit())

    result = retrieve_attachments("q", user_id=VALID_USER_ID, session_id=VALID_SESSION_ID, client=client)

    assert isinstance(result, DataSourceResult)
    assert result.source_type is SourceType.ATTACHMENTS
    [item] = result.results
    assert item["file_id"] == "file-1"
    assert item["filename"] == "report.pdf"
    assert item["text"] == "The chunk text."
    [citation] = result.citations
    assert citation.source_type is SourceType.ATTACHMENTS
    assert citation.title == "report.pdf"
    assert citation.url == ""
    assert citation.authors is None
    assert citation.year is None


def test_drops_a_chunk_whose_metadata_does_not_match_the_requested_session():
    client = Mock()
    client.retrieve.return_value = _response(
        _hit(session_id=VALID_SESSION_ID),
        _hit(session_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
    )

    result = retrieve_attachments("q", user_id=VALID_USER_ID, session_id=VALID_SESSION_ID, client=client)

    assert len(result.results) == 1
    assert result.results[0]["filename"] == "report.pdf"


def test_drops_a_chunk_whose_metadata_does_not_match_the_requested_user():
    client = Mock()
    client.retrieve.return_value = _response(
        _hit(user_id=VALID_USER_ID),
        _hit(user_id="someone-else"),
    )

    result = retrieve_attachments("q", user_id=VALID_USER_ID, session_id=VALID_SESSION_ID, client=client)

    assert len(result.results) == 1


def test_no_results_returns_none():
    client = Mock()
    client.retrieve.return_value = _response()
    result = retrieve_attachments("q", user_id=VALID_USER_ID, session_id=VALID_SESSION_ID, client=client)
    assert result is None


def test_all_results_filtered_out_returns_none():
    client = Mock()
    client.retrieve.return_value = _response(_hit(session_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"))
    result = retrieve_attachments("q", user_id=VALID_USER_ID, session_id=VALID_SESSION_ID, client=client)
    assert result is None


def test_client_error_returns_none_and_never_raises():
    client = Mock()
    client.retrieve.side_effect = RuntimeError("boto3 boom")
    result = retrieve_attachments("q", user_id=VALID_USER_ID, session_id=VALID_SESSION_ID, client=client)
    assert result is None


def test_error_log_never_contains_the_query_text(monkeypatch, caplog):
    import logging

    client = Mock()
    client.retrieve.side_effect = RuntimeError("boto3 boom")
    with caplog.at_level(logging.ERROR):
        retrieve_attachments(
            "a very secret query string", user_id=VALID_USER_ID, session_id=VALID_SESSION_ID, client=client,
        )
    assert "a very secret query string" not in caplog.text
