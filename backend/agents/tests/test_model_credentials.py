"""Regression: deployed runtimes must not pin a named AWS profile (ProfileNotFound in AgentCore => HTTP 424)."""

from __future__ import annotations

import pytest

import shared.utils as utils
from shared.config import AgentConfig


class _FakeModel:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs


@pytest.fixture
def capture(monkeypatch):
    monkeypatch.setattr(utils, "ChatBedrockConverse", _FakeModel)

    def _build(cfg: AgentConfig) -> dict:
        monkeypatch.setattr(utils, "config", cfg)
        return utils.get_model().kwargs

    return _build


def test_default_config_has_no_profile(monkeypatch) -> None:
    monkeypatch.delenv("CEI_AWS_PROFILE", raising=False)
    assert AgentConfig().aws_profile is None


@pytest.mark.parametrize("raw", ["", "   "])
def test_blank_profile_env_is_none(monkeypatch, raw: str) -> None:
    monkeypatch.setenv("CEI_AWS_PROFILE", raw)
    assert AgentConfig().aws_profile is None


def test_get_model_omits_profile_when_unset(monkeypatch, capture) -> None:
    monkeypatch.delenv("CEI_AWS_PROFILE", raising=False)
    kwargs = capture(AgentConfig())
    assert "credentials_profile_name" not in kwargs
    assert kwargs["region_name"] == "us-east-1"


def test_get_model_passes_profile_when_set(monkeypatch, capture) -> None:
    monkeypatch.setenv("CEI_AWS_PROFILE", "cei-project")
    kwargs = capture(AgentConfig())
    assert kwargs["credentials_profile_name"] == "cei-project"


def test_get_model_uses_deployment_default(monkeypatch, capture) -> None:
    monkeypatch.setenv("CEI_DEFAULT_MODEL_ID", "example.model-v2")
    assert capture(AgentConfig())['model_id'] == "example.model-v2"


def test_get_model_omits_guardrail_when_unset(monkeypatch, capture) -> None:
    monkeypatch.delenv("CEI_GUARDRAIL_ID", raising=False)
    monkeypatch.delenv("CEI_GUARDRAIL_VERSION", raising=False)
    assert "guardrails" not in capture(AgentConfig())


def test_get_model_wires_native_guardrail_when_configured(monkeypatch, capture) -> None:
    monkeypatch.setenv("CEI_GUARDRAIL_ID", "gr-123")
    monkeypatch.setenv("CEI_GUARDRAIL_VERSION", "3")
    kwargs = capture(AgentConfig())
    # B7: trace "disabled" so the Guardrail's own assessment never reaches the agent trace.
    assert kwargs["guardrails"] == {
        "guardrail_identifier": "gr-123", "guardrail_version": "3", "trace": "disabled",
    }


def test_get_model_requires_both_guardrail_id_and_version(monkeypatch, capture) -> None:
    monkeypatch.setenv("CEI_GUARDRAIL_ID", "gr-123")
    monkeypatch.delenv("CEI_GUARDRAIL_VERSION", raising=False)
    assert "guardrails" not in capture(AgentConfig())
