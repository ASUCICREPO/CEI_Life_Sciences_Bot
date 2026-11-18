from __future__ import annotations

import json

from cei_deploy import config


def _valid_raw():
    return {
        "region": "us-east-1",
        "defaultModelId": "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "routerModelId": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
        "blockedModelIds": ["anthropic.claude-fable-5"],
        "allowedEmailDomains": ["example.com"],
        "adminEmail": "admin@example.com",
        "limits": {"requestsPerSecond": 20, "maxRealtimeRunsPerUser": 2, "maxBackgroundRunsPerUser": 1},
        "webSearchEnabled": False,
        "allowLocalhostCallback": False,
        "toolPolicyMode": "LOG_ONLY",
        "repository": {"url": "https://github.com/acme/repo", "branch": "main"},
    }


def test_round_trip_through_disk(tmp_path):
    cfg = config.from_dict(_valid_raw())
    path = tmp_path / "cei.deploy.json"
    config.save(cfg, path)
    loaded = config.load(path)
    assert loaded.to_dict() == cfg.to_dict()
    assert config.validate(loaded) == []


def test_unknown_top_level_key_round_trips(tmp_path):
    raw = _valid_raw()
    raw["accountDeletionEnabled"] = True
    cfg = config.from_dict(raw)
    path = tmp_path / "cei.deploy.json"
    config.save(cfg, path)
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["accountDeletionEnabled"] is True


def test_validate_requires_region():
    cfg = config.from_dict({**_valid_raw(), "region": ""})
    errors = config.validate(cfg)
    assert any("region" in e for e in errors)


def test_validate_rejects_bad_region():
    cfg = config.from_dict({**_valid_raw(), "region": "not-a-region"})
    assert any("not a valid AWS Region" in e for e in config.validate(cfg))


def test_validate_rejects_non_github_repo_url():
    raw = _valid_raw()
    raw["repository"] = {"url": "https://gitlab.com/acme/repo", "branch": "main"}
    cfg = config.from_dict(raw)
    assert any("repository.url" in e for e in config.validate(cfg))


def test_validate_rejects_branch_with_dotdot():
    raw = _valid_raw()
    raw["repository"] = {"url": "https://github.com/acme/repo", "branch": "a..b"}
    cfg = config.from_dict(raw)
    assert any("repository.branch" in e for e in config.validate(cfg))


def test_validate_rejects_default_model_also_blocked():
    raw = _valid_raw()
    raw["blockedModelIds"] = [raw["defaultModelId"]]
    cfg = config.from_dict(raw)
    assert any("also in blockedModelIds" in e for e in config.validate(cfg))


def test_validate_rejects_bad_email():
    cfg = config.from_dict({**_valid_raw(), "adminEmail": "not-an-email"})
    assert any("adminEmail" in e for e in config.validate(cfg))


def test_validate_rejects_empty_domains():
    cfg = config.from_dict({**_valid_raw(), "allowedEmailDomains": []})
    assert any("allowedEmailDomains" in e for e in config.validate(cfg))


def test_validate_rejects_non_positive_limits():
    raw = _valid_raw()
    raw["limits"]["requestsPerSecond"] = 0
    cfg = config.from_dict(raw)
    assert any("limits.requestsPerSecond" in e for e in config.validate(cfg))


def test_validate_rejects_bad_tool_policy_mode():
    cfg = config.from_dict({**_valid_raw(), "toolPolicyMode": "SOMETHING_ELSE"})
    assert any("toolPolicyMode" in e for e in config.validate(cfg))


def test_load_missing_file_returns_none(tmp_path):
    assert config.load(tmp_path / "missing.json") is None


def test_default_config_path():
    assert config.default_config_path(config.repo_root_from_here()).parts[-2:] == ("backend", "cei.deploy.json")
