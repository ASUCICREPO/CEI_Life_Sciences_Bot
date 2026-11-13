"""Research follow-up turns carry the earlier conversation into triage and synthesize.

The earlier user text reaches the model as guard_content; the earlier assistant text and the
evidence stay plain text (B51/B51a).
"""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import Mock

from langchain_aws.chat_models.bedrock_converse import _messages_to_bedrock
from langchain_core.messages import AIMessage, HumanMessage, convert_to_messages

from shared.runtime.conversation import HISTORY_HEADER

PRIOR_USER = "GLP-1 agonist trials in humans"
PRIOR_ANSWER = "Semaglutide showed weight loss in STEP-1."
FOLLOW_UP = "What about in mice?"


def _messages() -> list:
    return [HumanMessage(content=PRIOR_USER), AIMessage(content=PRIOR_ANSWER), HumanMessage(content=FOLLOW_UP)]


def _blocks(call_messages: list[dict]) -> list[dict]:
    bedrock_messages, _ = _messages_to_bedrock(convert_to_messages(call_messages), system=None)
    return [block for message in bedrock_messages for block in message.get("content", [])]


def _guard_texts(blocks: list[dict]) -> list[str]:
    return [b["guardContent"]["text"]["text"] for b in blocks if "guardContent" in b]


def _plain(blocks: list[dict]) -> str:
    return "\n".join(b["text"] for b in blocks if set(b) == {"text"})


def test_triage_sends_earlier_turns(monkeypatch):
    from research.nodes import triage
    from research.state import TriagePlan

    raw = AIMessage(content="", usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})
    structured = Mock()
    structured.invoke.return_value = {"parsed": TriagePlan(selected_sources=[], reasoning="t"), "raw": raw}
    model = Mock()
    model.with_structured_output.return_value = structured
    monkeypatch.setattr("research.nodes.get_model", lambda **_: model)

    triage({"messages": _messages()})

    blocks = _blocks(structured.invoke.call_args.args[0])
    assert _guard_texts(blocks) == [PRIOR_USER, FOLLOW_UP]
    plain = _plain(blocks)
    assert HISTORY_HEADER.strip() in plain and PRIOR_ANSWER in plain


def test_synthesize_sends_earlier_turns_and_keeps_evidence_plain(monkeypatch):
    from research.nodes import synthesize
    from shared.schemas.citation import Citation, SourceType
    from shared.tools.base import DataSourceResult

    structured = Mock()
    structured.invoke.return_value = {"parsed": None, "raw": None, "parsing_error": "no tool call"}
    model = Mock()
    model.with_structured_output.return_value = structured
    model.invoke.return_value = AIMessage(content="answer")
    monkeypatch.setattr("research.nodes.get_model", lambda *a, **k: model)

    citation = Citation(
        source_id="PMID:1", source_type=SourceType.PUBMED, url="https://x", title="Mouse study",
        authors=["A Author"], year=2026, relevant_passage="Semaglutide reduced weight in mice.",
        retrieved_at=datetime.now(UTC),
    )
    retrieved = [DataSourceResult(source_type=SourceType.PUBMED, results=[], citations=[citation])]

    synthesize({"messages": _messages(), "query": FOLLOW_UP, "mode": "science", "retrieved_results": retrieved})

    for call in (structured.invoke.call_args, model.invoke.call_args):
        blocks = _blocks(call.args[0])
        assert _guard_texts(blocks) == [PRIOR_USER, FOLLOW_UP]
        plain = _plain(blocks)
        assert PRIOR_ANSWER in plain
        assert "Mouse study" in plain  # evidence is never guarded


def test_first_turn_is_unchanged(monkeypatch):
    from research.nodes import triage
    from research.state import TriagePlan

    raw = AIMessage(content="", usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})
    structured = Mock()
    structured.invoke.return_value = {"parsed": TriagePlan(selected_sources=[], reasoning="t"), "raw": raw}
    model = Mock()
    model.with_structured_output.return_value = structured
    monkeypatch.setattr("research.nodes.get_model", lambda **_: model)

    triage({"messages": [HumanMessage(content=FOLLOW_UP)]})

    blocks = _blocks(structured.invoke.call_args.args[0])
    assert _guard_texts(blocks) == [FOLLOW_UP]
    assert HISTORY_HEADER.strip() not in _plain(blocks)
