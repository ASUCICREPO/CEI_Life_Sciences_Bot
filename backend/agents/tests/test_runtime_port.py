"""Regression: every AgentCore entry point must default to port 8080.

AgentCore Runtime (HTTP protocol) sends /ping and /invocations only to port 8080 in the container. An entry
point that defaults to another port never answers, and AgentCore returns HTTP 424 after its startup wait
(Analysis 8083 and Pipeline 8084 did this). Local multi-agent runs set CEI_RUNTIME_PORT per agent instead.
"""

from __future__ import annotations

import importlib

import pytest

ENTRY_POINTS = ("main", "drafting_main", "analysis_main", "pipeline_main")


@pytest.mark.parametrize("module_name", ENTRY_POINTS)
def test_default_port_is_agentcore_port(monkeypatch, module_name: str) -> None:
    monkeypatch.delenv("CEI_RUNTIME_PORT", raising=False)
    module = importlib.import_module(module_name)
    assert module._runtime_port() == 8080


@pytest.mark.parametrize("module_name", ENTRY_POINTS)
def test_env_port_overrides_default(monkeypatch, module_name: str) -> None:
    monkeypatch.setenv("CEI_RUNTIME_PORT", "8083")
    module = importlib.import_module(module_name)
    assert module._runtime_port() == 8083
