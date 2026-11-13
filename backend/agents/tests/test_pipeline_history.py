"""Pipeline Manager follow-up turns carry earlier conversation into the plan/synthesize model calls.

Regression for the bug where ``plan`` and ``synthesize`` built their model-call content from only
``last_human_message(state)``, so a follow-up question had no memory of the prior turn.
"""
from __future__ import annotations

from unittest.mock import Mock

from langchain_aws.chat_models.bedrock_converse import _messages_to_bedrock
from langchain_core.messages import AIMessage, HumanMessage, convert_to_messages

from pipeline_manager import nodes
from pipeline_manager.state import PipelinePlan, PipelineStep, StepStatus, StepStatusValue
from shared.runtime.conversation import HISTORY_HEADER


def _to_bedrock(messages: list[dict]) -> tuple[list[dict], list[dict]]:
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


def _followup_state(**overrides) -> dict:
    base = {
        "messages": [
            HumanMessage(content="Research TP53 and draft a pitch"),
            AIMessage(content="Here is the initial research summary."),
            HumanMessage(content="Now narrow it to the EU market"),
        ],
        "mode": "science",
        "user_id": "u1",
        "session_id": "s1",
        "plan": None,
        "plan_approved": False,
        "steps": [],
        "synthesis": "",
        "retry_count": 0,
        "response_document": None,
        "trace": [],
    }
    base.update(overrides)
    return base


def test_plan_sends_earlier_turns_to_the_model(monkeypatch) -> None:
    raw = AIMessage(content="", usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})
    good_plan = PipelinePlan(
        summary="s",
        steps=[PipelineStep(step_id="step-1", agent="research", description="d", dependencies=[])],
        estimated_duration_seconds=10,
    )
    structured = Mock()
    structured.invoke.return_value = {"parsed": good_plan, "raw": raw}
    model = Mock()
    model.with_structured_output.return_value = structured
    monkeypatch.setattr(nodes, "get_model", lambda *a, **k: model)

    nodes.plan(_followup_state())

    messages = structured.invoke.call_args.args[0]
    bedrock_messages, _ = _to_bedrock(messages)
    # Current turn is the newest human message; it is sent, guarded, like the single-turn case.
    assert "Now narrow it to the EU market" in _guard_texts(bedrock_messages)
    # The earlier user turn is also guarded, and the history header + assistant reply are plain text.
    assert "Research TP53 and draft a pitch" in _guard_texts(bedrock_messages)
    plain = "\n".join(_plain_texts(bedrock_messages))
    assert HISTORY_HEADER.strip() in plain
    assert "Here is the initial research summary." in plain


def test_synthesize_sends_earlier_turns_to_the_model(monkeypatch) -> None:
    plan_obj = PipelinePlan(
        summary="s",
        steps=[PipelineStep(step_id="step-1", agent="research", description="d", dependencies=[])],
        estimated_duration_seconds=10,
    )
    steps = [StepStatus(
        step_id="step-1", agent="research", status=StepStatusValue.COMPLETED, result={"text": "EU findings"},
    )]

    captured: dict = {}

    def _fake_invoke_structured(model, schema, messages):
        captured["messages"] = messages
        from shared.schemas.response_blocks import ProseBlock, ResponseDocument, ResponseSection

        doc = ResponseDocument(
            title="t", summary="s",
            sections=[ResponseSection(heading="Answer", blocks=[ProseBlock(kind="prose", markdown="EU findings")])],
        )
        return doc, AIMessage(content="", usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}), None

    monkeypatch.setattr(nodes, "invoke_structured", _fake_invoke_structured)

    nodes.synthesize(_followup_state(plan=plan_obj, steps=steps))

    bedrock_messages, _ = _to_bedrock(captured["messages"])
    assert "Now narrow it to the EU market" in _guard_texts(bedrock_messages)
    assert "Research TP53 and draft a pitch" in _guard_texts(bedrock_messages)
    plain = "\n".join(_plain_texts(bedrock_messages))
    assert HISTORY_HEADER.strip() in plain
    assert "Here is the initial research summary." in plain
