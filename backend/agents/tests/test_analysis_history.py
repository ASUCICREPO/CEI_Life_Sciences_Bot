"""Analysis follow-up turns carry earlier chat history into the model calls that use the query.

A warm thread's `state["messages"]` already holds prior HumanMessage/AIMessage turns (added by
`begin_turn` in analysis_main.py via the `add_messages` reducer). These tests assert that `scope`
and `report` forward that history to the model via `user_turn_with_history`, with the user's
earlier text in guard_content blocks and the assistant's earlier text as plain text (B51/B51a).
"""
from __future__ import annotations

from unittest.mock import Mock

from langchain_core.messages import AIMessage, HumanMessage

from analysis.nodes import report, scope
from analysis.state import AnalysisType
from shared.runtime.conversation import HISTORY_HEADER
from shared.schemas.response_blocks import ProseBlock, ResponseDocument, ResponseSection


def _followup_messages() -> list:
    return [
        HumanMessage(content="Compare Acme Bio to us"),
        AIMessage(content="Acme Bio leads in oncology."),
        HumanMessage(content="What about their patent position?"),
    ]


def test_scope_sends_earlier_turns_to_the_model(monkeypatch) -> None:
    from analysis.state import ScopeProposal

    raw = AIMessage(content="", usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})
    structured = Mock()
    structured.invoke.return_value = {
        "parsed": ScopeProposal(
            analysis_type=AnalysisType.COMPETITOR, competitors=[], market=None, jurisdiction=None, focal_entity=None,
        ),
        "raw": raw,
    }
    model = Mock()
    model.with_structured_output.return_value = structured
    monkeypatch.setattr("analysis.nodes.get_model", lambda *a, **k: model)

    scope({
        "messages": _followup_messages(),
        "query": "What about their patent position?",
        "analysis_type": None,
        "scope_revision": 0,
    })

    messages = structured.invoke.call_args.args[0]
    user_content = messages[1]["content"]
    guard_texts = [b["text"] for b in user_content if b.get("type") == "guard_content"]
    plain_texts = [b["text"] for b in user_content if b.get("type") == "text"]

    assert guard_texts == ["Compare Acme Bio to us", "What about their patent position?"]
    assert any(HISTORY_HEADER in t for t in plain_texts)
    assert any("Acme Bio leads in oncology." in t for t in plain_texts)


def _sample_document() -> ResponseDocument:
    return ResponseDocument(
        title="Competitor analysis",
        summary="Acme Bio leads in patents.",
        key_metrics=[],
        sections=[
            ResponseSection(heading="Findings", blocks=[ProseBlock(kind="prose", markdown="Acme Bio leads.")]),
        ],
    )


def test_report_sends_earlier_turns_to_the_model(monkeypatch) -> None:
    doc = _sample_document()
    structured = Mock()
    structured.invoke.return_value = {"parsed": doc, "raw": None, "parsing_error": None}
    model = Mock()
    model.with_structured_output.return_value = structured
    monkeypatch.setattr("analysis.nodes.get_model", lambda *a, **k: model)
    monkeypatch.setattr("analysis.nodes.build_corpus", lambda *texts: " ".join(texts))
    monkeypatch.setattr("analysis.nodes.ground_document", lambda document, corpus: (document, []))
    monkeypatch.setattr("analysis.nodes.render_document_markdown", lambda document, **kw: "# report")

    state = {
        "messages": _followup_messages(),
        "mode": "science",
        "analysis_type": AnalysisType.COMPETITOR,
        "query": "What about their patent position?",
        "retrieved_results": [],
        "comparisons": [],
        "competitors": ["Acme Bio"],
        "focal_entity": "Our Co",
    }
    report(state)

    messages = structured.invoke.call_args.args[0]
    user_content = messages[1]["content"]
    guard_texts = [b["text"] for b in user_content if b.get("type") == "guard_content"]
    plain_texts = [b["text"] for b in user_content if b.get("type") == "text"]

    assert guard_texts == ["Compare Acme Bio to us", "What about their patent position?"]
    assert any(HISTORY_HEADER in t for t in plain_texts)
    assert any("Acme Bio leads in oncology." in t for t in plain_texts)


def test_report_fallback_path_also_sends_earlier_turns(monkeypatch) -> None:
    structured = Mock()
    structured.invoke.return_value = {"parsed": None, "raw": None, "parsing_error": "boom"}
    model = Mock()
    model.with_structured_output.return_value = structured
    model.invoke.return_value = AIMessage(content="fallback answer")
    monkeypatch.setattr("analysis.nodes.get_model", lambda *a, **k: model)

    state = {
        "messages": _followup_messages(),
        "mode": "science",
        "analysis_type": AnalysisType.COMPETITOR,
        "query": "What about their patent position?",
        "retrieved_results": [],
        "comparisons": [],
        "competitors": ["Acme Bio"],
        "focal_entity": "Our Co",
    }
    report(state)

    messages = model.invoke.call_args.args[0]
    user_content = messages[1]["content"]
    guard_texts = [b["text"] for b in user_content if b.get("type") == "guard_content"]
    plain_texts = [b["text"] for b in user_content if b.get("type") == "text"]

    assert guard_texts == ["Compare Acme Bio to us", "What about their patent position?"]
    assert any(HISTORY_HEADER in t for t in plain_texts)
