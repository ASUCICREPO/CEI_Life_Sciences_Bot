from __future__ import annotations

import pytest
from pydantic import ValidationError

from shared.config import AgentConfig


def test_local_default_allows_direct_mode(monkeypatch):
    monkeypatch.delenv("CEI_ENVIRONMENT", raising=False)
    monkeypatch.delenv("CEI_DIRECT_MODE", raising=False)
    cfg = AgentConfig()
    assert cfg.environment == "local"
    assert cfg.direct_mode is True


def test_deployed_refuses_direct_mode(monkeypatch):
    monkeypatch.setenv("CEI_ENVIRONMENT", "deployed")
    monkeypatch.delenv("CEI_DIRECT_MODE", raising=False)
    with pytest.raises(ValidationError, match="local CLI runs only"):
        AgentConfig()


def test_deployed_with_direct_mode_off_is_valid(monkeypatch):
    monkeypatch.setenv("CEI_ENVIRONMENT", "deployed")
    monkeypatch.setenv("CEI_DIRECT_MODE", "false")
    cfg = AgentConfig()
    assert cfg.environment == "deployed"
    assert cfg.direct_mode is False


def test_unknown_environment_is_rejected(monkeypatch):
    monkeypatch.setenv("CEI_ENVIRONMENT", "prod")
    with pytest.raises(ValidationError):
        AgentConfig()
