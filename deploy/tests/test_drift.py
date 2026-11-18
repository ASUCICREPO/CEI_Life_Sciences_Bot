"""Fails if the backend's tool-capable/D3-blocked model patterns drift from this tool's copies.

Parses both backend source files with `ast` (no import — the backend package has its own
dependencies/venv the deploy tool must not require) and compares the literal tuples.
"""
from __future__ import annotations

import ast
from pathlib import Path

from cei_deploy import models

REPO_ROOT = Path(__file__).resolve().parents[2]
MODEL_DISCOVERY_PATH = REPO_ROOT / "backend" / "agents" / "shared" / "runtime" / "model_discovery.py"
CONFIG_PATH = REPO_ROOT / "backend" / "agents" / "shared" / "config.py"


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
