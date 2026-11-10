from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from shared.runtime.input_guardrail import amask_user_messages, mask_user_messages
from shared.runtime.model_policy import GuardrailBlocked


def setup_client():
    client = Mock()
    def evaluate(**kwargs):
        text = kwargs["content"][0]["text"]["text"]
        return {"action": "GUARDRAIL_INTERVENED", "outputs": [{"text": text.replace("Alice", "{NAME}")}],
                "assessments": [{"sensitiveInformationPolicy": {"piiEntities": [{"action": "ANONYMIZED"}]}}]}
    client.apply_guardrail.side_effect = evaluate
    return client


def test_masks_current_and_prior_user_messages_only():
    client = setup_client()
    messages = [SystemMessage(content="system Alice"), HumanMessage(content="old Alice", id="old"),
                AIMessage(content="public Alice"), ToolMessage(content="tool Alice", tool_call_id="call"),
                HumanMessage(content="new Alice", id="new")]
    out = mask_user_messages(messages, client=client, guardrail_id="id", guardrail_version="1")
    assert out[1].content == "old {NAME}" and out[4].content == "new {NAME}"
    assert out[1].id == "old" and messages[1].content == "old Alice"
    assert out[0] is messages[0] and out[2] is messages[2] and out[3] is messages[3]
    assert client.apply_guardrail.call_count == 2


def test_masks_text_blocks_and_does_not_mutate_input():
    client = setup_client()
    message = HumanMessage(content=[{"type": "text", "text": "Alice"}, {"type": "text", "text": "hello"}])
    result = mask_user_messages([message], client=client, guardrail_id="id", guardrail_version="1")
    assert result[0].content[0]["text"] == "{NAME}"
    assert message.content[0]["text"] == "Alice"


@pytest.mark.parametrize("content", [[{"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}}],
                                      [{"type": "text", "text": "Alice", "unassessed": "secret"}]])
def test_unsupported_user_content_is_rejected(content):
    with pytest.raises(GuardrailBlocked):
        mask_user_messages([HumanMessage(content=content)], client=setup_client(), guardrail_id="id", guardrail_version="1")


def test_missing_configuration_and_intervention_fail_closed():
    client = setup_client()
    with pytest.raises(GuardrailBlocked):
        mask_user_messages([HumanMessage(content="Alice")], client=client, guardrail_id="", guardrail_version="1")
    client.apply_guardrail.assert_not_called()
    client.apply_guardrail.return_value = {"action": "GUARDRAIL_INTERVENED", "assessments": []}
    client.apply_guardrail.side_effect = None
    with pytest.raises(GuardrailBlocked):
        mask_user_messages([HumanMessage(content="Alice")], client=client, guardrail_id="id", guardrail_version="1")


def test_unassessed_human_message_metadata_fails_closed():
    with pytest.raises(GuardrailBlocked):
        mask_user_messages([HumanMessage(content="hi", additional_kwargs={"payload": "secret"})],
                           client=setup_client(), guardrail_id="id", guardrail_version="1")


@pytest.mark.asyncio
async def test_async_adapter_masks_user_input():
    result = await amask_user_messages([HumanMessage(content="Alice")], client=setup_client(), guardrail_id="id", guardrail_version="1")
    assert result[0].content == "{NAME}"
