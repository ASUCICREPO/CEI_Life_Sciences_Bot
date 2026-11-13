"""Follow-up turns must carry the earlier conversation into the model call (bug fix).

Each test builds a state with a prior HumanMessage/AIMessage turn plus a new HumanMessage
(the follow-up), mocks `get_model` like the existing drafting node tests, and asserts the
earlier turns reach the model: the prior user text as a `guard_content` block, the prior
assistant text as plain text (B51/B51a: only user-typed text is guarded).
"""
from __future__ import annotations

from unittest.mock import Mock

from langchain_aws.chat_models.bedrock_converse import _messages_to_bedrock
from langchain_core.messages import AIMessage, HumanMessage, convert_to_messages

from drafting.nodes import RouteDecision, chat_answer, draft, revise, route_request
from drafting.state import DocumentType
from shared.runtime.conversation import HISTORY_HEADER


def _history_state(**overrides) -> dict:
    state = {
        "messages": [
            HumanMessage(content="Draft a grant about CRISPR"),
            AIMessage(content="Here is the draft about CRISPR."),
            HumanMessage(content="Make section 2 shorter"),
        ],
        "document_type": DocumentType.GRANT,
        "guidelines": [],
        "research_findings": [],
        "draft": "",
        "outline": "",
        "outline_approved": False,
    }
    state.update(overrides)
    return state


def _to_bedrock(messages: list[dict]) -> list[dict]:
    bedrock_messages, _ = _messages_to_bedrock(convert_to_messages(messages), system=None)
    return bedrock_messages


def _guard_texts(bedrock_messages: list[dict]) -> list[str]:
    return [
        block["guardContent"]["text"]["text"]
        for message in bedrock_messages
        for block in message.get("content", [])
        if "guardContent" in block
    ]


def _plain_texts(bedrock_messages: list[dict]) -> list[str]:
    return [
        block["text"]
        for message in bedrock_messages
        for block in message.get("content", [])
        if set(block) == {"text"}
    ]


def test_route_request_sends_earlier_turns_to_the_model(monkeypatch) -> None:
    decision = RouteDecision(
        intent="revise", document_type=DocumentType.GRANT, needs_outline_approval=False,
        reasoning="follow-up edit",
    )
    structured = Mock()
    structured.invoke.return_value = {"parsed": decision, "raw": AIMessage(content="")}
    model = Mock()
    model.with_structured_output.return_value = structured
    monkeypatch.setattr("drafting.nodes.get_model", lambda *a, **k: model)

    route_request(_history_state(draft="Existing draft text."))

    messages = structured.invoke.call_args.args[0]
    user_message = next(m for m in messages if m["role"] == "user")
    bedrock_messages = _to_bedrock([user_message])
    assert _guard_texts(bedrock_messages) == ["Draft a grant about CRISPR", "Make section 2 shorter"]
    assert any(HISTORY_HEADER.strip() in t for t in _plain_texts(bedrock_messages))
    assert any("Here is the draft about CRISPR." in t for t in _plain_texts(bedrock_messages))


def test_chat_answer_sends_earlier_turns_to_the_model(monkeypatch) -> None:
    model = Mock()
    model.invoke.return_value = AIMessage(content="It covers CRISPR delivery.")
    monkeypatch.setattr("drafting.nodes.get_model", lambda *a, **k: model)

    result = chat_answer(_history_state())

    messages = model.invoke.call_args.args[0]
    bedrock_messages = _to_bedrock(messages)
    assert _guard_texts(bedrock_messages) == ["Draft a grant about CRISPR", "Make section 2 shorter"]
    assert any("Here is the draft about CRISPR." in t for t in _plain_texts(bedrock_messages))
    assert result["messages"][0].content == "It covers CRISPR delivery."


def test_draft_sends_earlier_turns_to_the_model(monkeypatch) -> None:
    structured_model = Mock()
    structured_model.invoke.return_value = {"parsed": None, "raw": None, "parsing_error": "no tool call"}
    model = Mock()
    model.with_structured_output.return_value = structured_model
    model.invoke.return_value = AIMessage(content="Plain draft text.")
    monkeypatch.setattr("drafting.nodes.get_model", lambda *a, **k: model)

    draft(_history_state(chart_data={}))

    messages = model.invoke.call_args.args[0]
    bedrock_messages = _to_bedrock(messages)
    assert _guard_texts(bedrock_messages) == ["Draft a grant about CRISPR", "Make section 2 shorter"]
    assert any("Here is the draft about CRISPR." in t for t in _plain_texts(bedrock_messages))


def test_revise_sends_earlier_turns_to_the_model(monkeypatch) -> None:
    structured_model = Mock()
    structured_model.invoke.return_value = {"parsed": None, "raw": None, "parsing_error": "no tool call"}
    model = Mock()
    model.with_structured_output.return_value = structured_model
    model.invoke.return_value = AIMessage(content="Revised text.")
    monkeypatch.setattr("drafting.nodes.get_model", lambda *a, **k: model)

    state = _history_state(
        draft="# Grant\n\nSection 2 is long.", revision_feedback="Make section 2 shorter", chart_data={},
    )
    revise(state)

    messages = model.invoke.call_args.args[0]
    bedrock_messages = _to_bedrock(messages)
    guard_texts = _guard_texts(bedrock_messages)
    assert "Draft a grant about CRISPR" in guard_texts
    assert "Make section 2 shorter" in guard_texts
    assert any("Here is the draft about CRISPR." in t for t in _plain_texts(bedrock_messages))
