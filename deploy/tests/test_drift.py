"""Fails if the backend's tool-capable/D3-blocked model patterns, or its `is_blocked` behaviour,
drift from this tool's copies.

The pattern-tuple tests parse the backend source with `ast` (no import — the backend package has
its own dependencies/venv the deploy tool must not require) and compare the literal tuples. The
`is_blocked` behaviour test imports `backend/agents/shared/runtime/model_policy.py` directly by
file path, since that module uses only the standard library.
"""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

from cei_deploy import models

REPO_ROOT = Path(__file__).resolve().parents[2]
MODEL_DISCOVERY_PATH = REPO_ROOT / "backend" / "agents" / "shared" / "runtime" / "model_discovery.py"
CONFIG_PATH = REPO_ROOT / "backend" / "agents" / "shared" / "config.py"
MODEL_POLICY_PATH = REPO_ROOT / "backend" / "agents" / "shared" / "runtime" / "model_policy.py"


def _literal_tuple_assignment(path: Path, name: str):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id == name:
                return ast.literal_eval(node.value)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
            return ast.literal_eval(node.value)
    raise AssertionError('Could not find assignment to "%s" in %s' % (name, path))


def test_tool_capable_patterns_match_backend():
    backend_patterns = _literal_tuple_assignment(MODEL_DISCOVERY_PATH, "_TOOL_CAPABLE_MODEL_ID_PATTERNS")
    assert tuple(backend_patterns) == models._TOOL_CAPABLE_MODEL_ID_PATTERNS


def test_d3_blocked_patterns_match_backend():
    backend_patterns = _literal_tuple_assignment(CONFIG_PATH, "D3_BLOCKED_MODEL_PATTERNS")
    assert tuple(backend_patterns) == models._D3_BLOCKED_MODEL_PATTERNS


def _load_backend_model_policy():
    spec = importlib.util.spec_from_file_location("backend_model_policy_drift_check", MODEL_POLICY_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_IS_BLOCKED_CASES = (
    # (model_id, blocklist)
    ("anthropic.claude-sonnet-x", ["anthropic.claude-sonnet-x"]),  # unprefixed pattern, unprefixed id
    ("us.anthropic.claude-sonnet-x", ["anthropic.claude-sonnet-x"]),  # unprefixed pattern blocks every prefix
    ("global.anthropic.claude-sonnet-x", ["anthropic.claude-sonnet-x"]),
    ("us.anthropic.claude-sonnet-x", ["us.anthropic.claude-sonnet-x"]),  # prefixed pattern, same prefix
    ("global.anthropic.claude-sonnet-x", ["us.anthropic.claude-sonnet-x"]),  # prefixed pattern, other prefix
    ("anthropic.claude-sonnet-x", ["us.anthropic.claude-sonnet-x"]),  # prefixed pattern, bare id
    ("anthropic.claude-haiku-x", ["anthropic.claude-sonnet-x"]),  # no match
    ("anthropic.claude-sonnet-y", ["anthropic.claude-sonnet-*"]),  # glob pattern
    ("us.anthropic.claude-sonnet-y", ["anthropic.claude-sonnet-*"]),  # glob, unprefixed pattern, prefixed id
    ("us.anthropic.claude-sonnet-y", ["us.anthropic.claude-sonnet-*"]),  # glob, prefixed pattern
    ("global.anthropic.claude-sonnet-y", ["us.anthropic.claude-sonnet-*"]),  # glob, prefixed, other prefix
    ("anthropic.claude-fable-5-2", ["anthropic.claude-fable-5*"]),  # D3 pattern
    ("us.anthropic.claude-fable-5-2", ["anthropic.claude-fable-5*"]),  # D3 pattern, every prefix
    ("arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-sonnet-x", ["anthropic.claude-sonnet-x"]),  # ARN
    ("", ["anthropic.claude-sonnet-x"]),  # empty id
    ("   ", ["anthropic.claude-sonnet-x"]),  # blank id
    (None, ["anthropic.claude-sonnet-x"]),  # non-string id
)


@pytest.mark.parametrize("model_id,blocklist", _IS_BLOCKED_CASES)
def test_is_blocked_matches_backend_behaviour(model_id, blocklist):
    # ADR: drift test (B113) | Rationale: the deploy copy of is_blocked must behave exactly like
    # backend/agents/shared/runtime/model_policy.is_blocked, loaded by file path since that module
    # has no third-party imports | NOTE: if this fails only because the backend side still has the
    # pre-B113 rule (base-id-only matching, no prefix asymmetry), it is mid-edit elsewhere -- rerun
    # once that edit lands.
    backend = _load_backend_model_policy()
    assert models.is_blocked(model_id, blocklist) == backend.is_blocked(model_id, blocklist)
