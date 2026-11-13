"""B1xx: route_request's intent/document-type/outline-approval decisions."""
from __future__ import annotations

from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import ValidationError

from drafting.nodes import RouteDecision, route_after_request, route_request
from drafting.state import DocumentType


def _mock_route_model(monkeypatch, decision: RouteDecision) -> None:
    structured = Mock()
    structured.invoke.return_value = {"parsed": decision, "raw": AIMessage(content="")}
    model = Mock()
    model.with_structured_output.return_value = structured
    monkeypatch.setattr("drafting.nodes.get_model", lambda *a, **k: model)


def _state(message: str, **overrides) -> dict:
    state = {
        "messages": [HumanMessage(content=message)],
        "document_type": DocumentType.WHITEPAPER,
        "draft": "",
        "outline": "",
        "outline_approved": False,
    }
    state.update(overrides)
    return state


def test_new_document_needs_outline_approval_sets_flag_and_resets_prior_state(monkeypatch) -> None:
    decision = RouteDecision(
        intent="new_document", document_type=DocumentType.GRANT, needs_outline_approval=True,
        reasoning="multi-section grant",
    )
    _mock_route_model(monkeypatch, decision)

    result = route_request(_state("Write a grant about GLP-1", draft="old draft", outline="old outline"))

    assert result["intent"] == "new_document"
    assert result["document_type"] == DocumentType.GRANT
    assert result["needs_outline_approval"] is True
    assert result["draft"] == ""  # a brand new document drops the prior one
    assert result["outline"] == ""


def test_follow_up_edit_does_not_require_outline_approval(monkeypatch) -> None:
    decision = RouteDecision(
        intent="revise", document_type=DocumentType.WHITEPAPER, needs_outline_approval=False,
        reasoning="small wording change",
    )
    _mock_route_model(monkeypatch, decision)

    result = route_request(_state("Make the second paragraph shorter", draft="Existing whitepaper text."))

    assert result["intent"] == "revise"
    assert result["revision_feedback"] == "Make the second paragraph shorter"
    assert "needs_outline_approval" not in result  # unchanged from the prior turn's carried-forward value


def test_revise_without_a_prior_draft_falls_back_to_new_document(monkeypatch) -> None:
    decision = RouteDecision(
        intent="revise", document_type=DocumentType.WHITEPAPER, reasoning="misread as a revision",
    )
    _mock_route_model(monkeypatch, decision)

    result = route_request(_state("Make it shorter", draft=""))

    assert result["intent"] == "new_document"


def test_export_without_a_prior_draft_falls_back_to_new_document(monkeypatch) -> None:
    decision = RouteDecision(
        intent="export", document_type=DocumentType.WHITEPAPER, export_formats=["pdf"], reasoning="no draft yet",
    )
    _mock_route_model(monkeypatch, decision)

    result = route_request(_state("Send me the PDF", draft=""))

    assert result["intent"] == "new_document"


def test_approve_outline_with_no_feedback_approves(monkeypatch) -> None:
    decision = RouteDecision(
        intent="approve_outline", document_type=DocumentType.GRANT, outline_feedback=None, reasoning="looks good",
    )
    _mock_route_model(monkeypatch, decision)

    result = route_request(_state("Looks good, go ahead", outline="- Intro\n- Methods", outline_approved=False))

    assert result["intent"] == "approve_outline"
    assert result["outline_approved"] is True
    assert route_after_request(result) == "draft"


def test_approve_outline_with_feedback_requests_a_new_outline(monkeypatch) -> None:
    decision = RouteDecision(
        intent="approve_outline", document_type=DocumentType.GRANT, outline_feedback="add a budget section",
        reasoning="wants a change",
    )
    _mock_route_model(monkeypatch, decision)

    result = route_request(_state("Can you add a budget section?", outline="- Intro\n- Methods", outline_approved=False))

    assert result["intent"] == "approve_outline"
    assert result["outline_correction"] == "add a budget section"
    assert result["outline_approved"] is False
    assert route_after_request(result) == "outline"


def test_approve_outline_intent_without_a_pending_outline_is_treated_as_new_document(monkeypatch) -> None:
    decision = RouteDecision(
        intent="approve_outline", document_type=DocumentType.WHITEPAPER, reasoning="misclassified",
    )
    _mock_route_model(monkeypatch, decision)

    result = route_request(_state("Looks good", outline="", outline_approved=False))

    assert result["intent"] == "new_document"


def test_payload_document_type_overrides_the_router(monkeypatch) -> None:
    decision = RouteDecision(intent="new_document", document_type=DocumentType.WHITEPAPER, reasoning="default guess")
    _mock_route_model(monkeypatch, decision)

    result = route_request(_state("Draft something", payload_document_type="latex"))

    assert result["document_type"] == DocumentType.LATEX


def test_non_interactive_caller_skips_the_llm_call(monkeypatch) -> None:
    def _boom(*_a, **_k):
        raise AssertionError("non-interactive callers (Pipeline) must not call the router LLM")
    monkeypatch.setattr("drafting.nodes.get_model", _boom)

    result = route_request(_state("Draft a VC pitch", document_type=DocumentType.VC_PITCH,
                                   skip_outline_approval=True, export_formats=["md", "txt"]))

    assert result["intent"] == "new_document"
    assert result["document_type"] == DocumentType.VC_PITCH
    assert result["needs_outline_approval"] is False
    assert "md" in result["export_formats"]


def test_route_after_request_routes_each_intent() -> None:
    assert route_after_request({"intent": "revise"}) == "revise"
    assert route_after_request({"intent": "export"}) == "export"
    assert route_after_request({"intent": "chat"}) == "chat"
    assert route_after_request({"intent": "new_document"}) == "research"


def test_export_file_component_validates_export_id() -> None:
    from shared.schemas.cei_ui import ExportFileComponent

    valid_id = "6f1c2d3e-0000-4000-8000-000000000001_" + "a" * 32
    component = ExportFileComponent(id="export-0", export_id=valid_id, filename="report.pdf", format="pdf", byte_size=10)
    assert component.export_id == valid_id

    with pytest.raises(ValidationError):
        ExportFileComponent(id="export-0", export_id="not-an-export-id", filename="report.pdf", format="pdf", byte_size=10)
