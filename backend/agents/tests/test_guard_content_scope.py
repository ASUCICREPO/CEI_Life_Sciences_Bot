"""B51/B51a: guardContent covers only user-typed text, never retrieved evidence/memory/system prompts.

Each "per agent" test below replicates the existing node-level mocking pattern (mock
``get_model``, run the node, capture the message list passed to ``.invoke()``), then
runs that captured message list through the real langchain_aws Converse message
converter -- the same function the production ``GuardScopedChatBedrockConverse``
checks against -- to assert on the actual wire-level request shape, with no AWS call.
"""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from langchain_aws.chat_models.bedrock_converse import _messages_to_bedrock
from langchain_core.messages import AIMessage, HumanMessage, convert_to_messages

from shared.runtime.guarded_input import (
    GuardContentRequiredError,
    GuardScopedChatBedrockConverse,
    content_blocks,
    guard_human_messages,
    user_turn,
)


def _to_bedrock(messages: list[dict]) -> tuple[list[dict], list[dict]]:
    """Convert a node's raw `[{"role": ..., "content": ...}, ...]` call into the wire request."""
    return _messages_to_bedrock(convert_to_messages(messages), system=None)


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


# ── content_blocks / user_turn / guard_human_messages ───────────────────────


def test_user_turn_wraps_only_the_user_text():
    blocks = user_turn("my query", before="Query: ", after="\n\nEvidence: stuff")
    assert blocks == [
        {"type": "text", "text": "Query: "},
        {"type": "guard_content", "text": "my query"},
        {"type": "text", "text": "\n\nEvidence: stuff"},
    ]


def test_content_blocks_empty_segments_are_dropped():
    from shared.runtime.guarded_input import Guarded

    blocks = content_blocks("", Guarded(""), "kept")
    assert blocks == [{"type": "text", "text": "kept"}]


def test_guard_human_messages_leaves_other_roles_untouched():
    messages = [
        HumanMessage(content="typed by the user"),
        AIMessage(content="agent reply, not user text"),
    ]
    converted = guard_human_messages(messages)
    assert converted[0].content == [{"type": "guard_content", "text": "typed by the user"}]
    assert converted[1] is messages[1]


# ── GuardScopedChatBedrockConverse: fail-closed / opt-out / pass ────────────


class _FakeBedrockClient:
    def __init__(self):
        self.calls: list[dict] = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "output": {"message": {"content": [{"text": "ok"}]}},
            "usage": {"inputTokens": 1, "outputTokens": 1},
        }


def _guard_scoped_model(**extra):
    fake_client = _FakeBedrockClient()
    model = GuardScopedChatBedrockConverse(
        model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
        region_name="us-east-1",
        client=fake_client,
        bedrock_client=fake_client,
        guardrails={"guardrail_identifier": "gr-1", "guardrail_version": "1", "trace": "disabled"},
        **extra,
    )
    return model, fake_client


def test_guarded_call_with_no_guard_content_and_no_opt_out_raises():
    model, fake_client = _guard_scoped_model()
    with pytest.raises(GuardContentRequiredError):
        model.invoke([{"role": "user", "content": "plain text, no guard_content block"}])
    assert fake_client.calls == []  # fails before any Bedrock call


def test_guarded_call_with_guard_content_succeeds():
    model, fake_client = _guard_scoped_model()
    response = model.invoke([{"role": "user", "content": user_turn("hello")}])
    assert response.content == "ok"
    assert fake_client.calls  # the real converse() call went through


def test_guarded_call_with_user_text_false_opt_out_skips_the_check():
    model, fake_client = _guard_scoped_model(user_text_required=False)
    response = model.invoke([{"role": "user", "content": "plain text, no guard_content block"}])
    assert response.content == "ok"
    assert fake_client.calls


def test_guarded_stream_also_fails_closed():
    model, fake_client = _guard_scoped_model()
    with pytest.raises(GuardContentRequiredError):
        list(model.stream([{"role": "user", "content": "plain text, no guard_content block"}]))
    assert fake_client.calls == []


def test_no_guardrail_configured_behaves_exactly_as_before(monkeypatch):
    """shared/utils.get_model() with no guardrail env must not use the guard-scoped subclass at all."""
    import shared.utils as utils
    from shared.config import AgentConfig

    monkeypatch.delenv("CEI_GUARDRAIL_ID", raising=False)
    monkeypatch.delenv("CEI_GUARDRAIL_VERSION", raising=False)
    monkeypatch.setattr(utils, "config", AgentConfig())
    fake_client = _FakeBedrockClient()
    # get_model() does not accept client=; build the exact class it would pick, directly.
    assert utils.ChatBedrockConverse is not utils.GuardScopedChatBedrockConverse
    built = utils.ChatBedrockConverse(
        model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
        region_name="us-east-1",
        client=fake_client,
        bedrock_client=fake_client,
    )
    assert not isinstance(built, utils.GuardScopedChatBedrockConverse)
    response = built.invoke([{"role": "user", "content": "plain text, never guarded"}])
    assert response.content == "ok"


# ── per-agent: the actual node builds guardContent around user text only ───


def test_analysis_scope_guards_only_the_query(monkeypatch):
    from analysis.nodes import scope
    from analysis.state import AnalysisType, ScopeProposal

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

    scope({"query": "Compare Acme Bio to us", "analysis_type": None, "scope_revision": 0})

    messages = structured.invoke.call_args.args[0]
    bedrock_messages, _ = _to_bedrock(messages)
    assert _guard_texts(bedrock_messages) == ["Compare Acme Bio to us"]


def test_drafting_outline_guards_query_and_correction(monkeypatch):
    from drafting.nodes import outline
    from drafting.state import DocumentType

    model = Mock()
    model.invoke.return_value = AIMessage(content="- Intro")
    monkeypatch.setattr("drafting.nodes.get_model", lambda **_: model)

    state = {
        "messages": [HumanMessage(content="Write a grant about GLP-1")],
        "document_type": DocumentType.GRANT,
        "guidelines": [],
        "research_findings": [],
        "outline_correction": "add an appendix",
        "outline_revision": 1,
    }
    outline(state)

    messages = model.invoke.call_args.args[0]
    bedrock_messages, _ = _to_bedrock(messages)
    assert _guard_texts(bedrock_messages) == ["Write a grant about GLP-1", "add an appendix"]


def test_pipeline_manager_plan_guards_only_the_task(monkeypatch):
    from pipeline_manager import nodes
    from pipeline_manager.state import PipelinePlan, PipelineStep

    raw = AIMessage(content="", usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})
    structured = Mock()
    structured.invoke.return_value = {
        "parsed": PipelinePlan(
            summary="s",
            steps=[PipelineStep(step_id="step-1", agent="research", description="d", dependencies=[])],
            estimated_duration_seconds=10,
        ),
        "raw": raw,
    }
    model = Mock()
    model.with_structured_output.return_value = structured
    monkeypatch.setattr(nodes, "get_model", lambda *a, **k: model)

    nodes.plan({"messages": [HumanMessage(content="Find GLP-1 competitors")]})

    messages = structured.invoke.call_args.args[0]
    bedrock_messages, _ = _to_bedrock(messages)
    assert _guard_texts(bedrock_messages) == ["Find GLP-1 competitors"]


def test_research_triage_guards_query_and_clarification(monkeypatch):
    from research.nodes import triage
    from research.state import TriagePlan

    raw = AIMessage(content="", usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})
    structured = Mock()
    structured.invoke.return_value = {"parsed": TriagePlan(selected_sources=[], reasoning="t"), "raw": raw}
    model = Mock()
    model.with_structured_output.return_value = structured
    monkeypatch.setattr("research.nodes.get_model", lambda **_: model)

    triage({
        "messages": [HumanMessage(content="GLP-1 trials")],
        "clarification_answer": "Use only pubmed.",
    })

    messages = structured.invoke.call_args.args[0]
    bedrock_messages, _ = _to_bedrock(messages)
    assert _guard_texts(bedrock_messages) == ["GLP-1 trials", "Use only pubmed."]


def test_research_synthesize_evidence_stays_plain_text(monkeypatch):
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
        source_id="PMID:1", source_type=SourceType.PUBMED, url="https://x", title="Secret Author Name",
        authors=["Secret Author Name"], year=2026, relevant_passage="Dr. Secret Author Name's contact info.",
        retrieved_at=datetime.now(UTC),
    )
    retrieved = [DataSourceResult(source_type=SourceType.PUBMED, results=[], citations=[citation])]

    synthesize({"query": "aspirin and GLP-1", "mode": "science", "retrieved_results": retrieved})

    messages = model.invoke.call_args.args[0]
    bedrock_messages, _ = _to_bedrock(messages)
    assert _guard_texts(bedrock_messages) == ["aspirin and GLP-1"]
    plain = "\n".join(_plain_texts(bedrock_messages))
    assert "Secret Author Name" in plain  # evidence/citations are never masked (B51a)


def test_tool_agent_query_guards_the_human_message(monkeypatch):
    import asyncio

    from langgraph.checkpoint.memory import InMemorySaver

    from tool_agent.graph import compile_graph
    from tool_agent.profile import load_profile

    class _FakeTool:
        name = "pubmed"

        async def ainvoke(self, arguments):
            return {"results": []}

    captured: list = []

    class _FakeModel:
        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages):
            captured.append(messages)
            return AIMessage(content="done")

    graph = compile_graph(load_profile("pubmed"), InMemorySaver(), model=_FakeModel(), tool=_FakeTool())
    asyncio.run(graph.ainvoke(
        {"messages": [HumanMessage(content="What does PubMed say about GLP-1?")], "tool_rounds": 0},
        {"configurable": {"thread_id": "t1"}},
    ))

    bedrock_messages, _ = _messages_to_bedrock(list(captured[0]), system=None)
    assert _guard_texts(bedrock_messages) == ["What does PubMed say about GLP-1?"]
