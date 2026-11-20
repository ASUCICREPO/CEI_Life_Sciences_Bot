from unittest.mock import Mock

import pytest

from shared.runtime.model_policy import (GuardrailBlocked, ModelPolicyError, is_blocked, mask_user_input,
                                         permitted_models, select_mode, select_model)


def test_blocklist_matches_cross_region_alias_and_arn():
    models = ["us.anthropic.claude-fable-5-v1", "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-fable-5-v1",
              "us.anthropic.claude-sonnet-v1"]
    assert permitted_models(models, ["anthropic.claude-fable-5*"]) == [models[-1]]


def test_unprefixed_pattern_blocks_every_profile_and_arn():
    # D3 regression: an unprefixed pattern must keep blocking the model under every profile prefix.
    pattern = ["anthropic.claude-fable-5*"]
    assert is_blocked("anthropic.claude-fable-5-v1", pattern)
    assert is_blocked("us.anthropic.claude-fable-5-v1", pattern)
    assert is_blocked("global.anthropic.claude-fable-5-v1", pattern)
    assert is_blocked("eu.anthropic.claude-fable-5-v1", pattern)
    assert is_blocked("apac.anthropic.claude-fable-5-v1", pattern)
    assert is_blocked("arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-fable-5-v1", pattern)
    assert is_blocked("arn:aws:bedrock:us-east-1:123:inference-profile/us.anthropic.claude-fable-5-v1", pattern)


def test_prefixed_pattern_blocks_only_that_profile():
    pattern = ["global.anthropic.claude-x"]
    assert is_blocked("global.anthropic.claude-x", pattern)
    assert is_blocked("arn:aws:bedrock:us-east-1:123:inference-profile/global.anthropic.claude-x", pattern)
    assert not is_blocked("us.anthropic.claude-x", pattern)
    assert not is_blocked("anthropic.claude-x", pattern)
    assert not is_blocked("eu.anthropic.claude-x", pattern)


def test_prefixed_glob_pattern_matches_only_its_own_profile():
    pattern = ["us.anthropic.claude-x*"]
    assert is_blocked("us.anthropic.claude-x-v1", pattern)
    assert not is_blocked("global.anthropic.claude-x-v1", pattern)
    assert not is_blocked("anthropic.claude-x-v1", pattern)


def test_empty_or_non_string_model_id_is_blocked():
    assert is_blocked("", ["anything"])
    assert is_blocked("   ", ["anything"])
    assert is_blocked(None, [])  # type: ignore[arg-type]
    assert is_blocked(123, [])  # type: ignore[arg-type]


def test_permitted_models_keeps_other_profiles_when_only_one_prefix_blocked():
    models = ["global.anthropic.claude-x", "us.anthropic.claude-x", "anthropic.claude-x"]
    assert permitted_models(models, ["global.anthropic.claude-x"]) == models[1:]


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
