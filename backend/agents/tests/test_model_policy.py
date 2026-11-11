from unittest.mock import Mock

import pytest

from shared.runtime.model_policy import (GuardrailBlocked, ModelPolicyError, mask_user_input,
                                         permitted_models, select_mode, select_model)


def test_blocklist_matches_cross_region_alias_and_arn():
    models = ["us.anthropic.claude-fable-5-v1", "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-fable-5-v1",
              "us.anthropic.claude-sonnet-v1"]
    assert permitted_models(models, ["anthropic.claude-fable-5*"]) == [models[-1]]


def test_explicit_blocked_model_never_falls_back_to_default():
    with pytest.raises(ModelPolicyError):
        select_model("bad", user_default="good", global_default="good", available=["bad", "good"], blocklist=["bad"])
    assert select_model(None, user_default="good", global_default="other", available=["good"], blocklist=[]) == "good"


def test_mode_validation():
    assert select_mode("business") == "business"
    with pytest.raises(ModelPolicyError):
        select_mode("admin")


def test_guardrail_masks_only_user_input():
    client = Mock()
    client.apply_guardrail.return_value = {"action": "GUARDRAIL_INTERVENED", "outputs": [{"text": "Hello {NAME}"}],
        "assessments": [{"sensitiveInformationPolicy": {"piiEntities": [{"action": "ANONYMIZED"}]}}]}
    assert mask_user_input("Hello Alice", client=client, guardrail_id="id", guardrail_version="1") == "Hello {NAME}"
    assert client.apply_guardrail.call_args.kwargs["source"] == "INPUT"


def test_no_intervention_returns_original():
    client = Mock(apply_guardrail=Mock(return_value={"action": "NONE", "outputs": []}))
    assert mask_user_input("hello", client=client, guardrail_id="id", guardrail_version="1") == "hello"


@pytest.mark.parametrize("reply", [
    {"action": "GUARDRAIL_INTERVENED", "outputs": [{"text": "refusal"}], "assessments": [{"topicPolicy": {"topics": [{"action": "BLOCKED"}]}}]},
    {"action": "GUARDRAIL_INTERVENED", "outputs": [{"text": "untrusted"}], "assessments": []},
    {"action": "GUARDRAIL_INTERVENED", "outputs": [], "assessments": [{"action": "ANONYMIZED"}]},
    {"action": "UNKNOWN"},
])
def test_blocked_or_ambiguous_response_never_returns_input(reply):
    with pytest.raises(GuardrailBlocked):
        mask_user_input("secret", client=Mock(apply_guardrail=Mock(return_value=reply)), guardrail_id="id", guardrail_version="1")


def test_missing_guardrail_fails_before_model_or_guardrail_call():
    client = Mock()
    with pytest.raises(GuardrailBlocked):
        mask_user_input("secret", client=client, guardrail_id="", guardrail_version="1")
    client.apply_guardrail.assert_not_called()
