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
