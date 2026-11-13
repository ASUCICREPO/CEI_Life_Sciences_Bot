from shared.config import D3_BLOCKED_MODEL_PATTERNS, AgentConfig


def test_model_blocklist_defaults_to_d3_patterns_only(monkeypatch):
    monkeypatch.delenv("CEI_BLOCKED_MODELS", raising=False)
    assert AgentConfig().model_blocklist == list(D3_BLOCKED_MODEL_PATTERNS)


def test_model_blocklist_merges_env_patterns_with_d3(monkeypatch):
    monkeypatch.setenv("CEI_BLOCKED_MODELS", "vendor.restricted-*, other.blocked-model")
    blocklist = AgentConfig().model_blocklist
    assert blocklist[: len(D3_BLOCKED_MODEL_PATTERNS)] == list(D3_BLOCKED_MODEL_PATTERNS)
    assert blocklist[len(D3_BLOCKED_MODEL_PATTERNS):] == ["vendor.restricted-*", "other.blocked-model"]


def test_model_blocklist_ignores_blank_entries(monkeypatch):
    monkeypatch.setenv("CEI_BLOCKED_MODELS", " , ,vendor.blocked,")
    assert AgentConfig().model_blocklist == [*D3_BLOCKED_MODEL_PATTERNS, "vendor.blocked"]


def test_guardrail_id_and_version_default_to_empty(monkeypatch):
    monkeypatch.delenv("CEI_GUARDRAIL_ID", raising=False)
    monkeypatch.delenv("CEI_GUARDRAIL_VERSION", raising=False)
    cfg = AgentConfig()
    assert cfg.guardrail_id == "" and cfg.guardrail_version == ""
