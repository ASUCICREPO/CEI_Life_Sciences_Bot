"""F19/B109: boto3-only AgentCore Memory deletion, and a guard that the deletion Lambdas import
only shared code that the python-deps layer ships (shared/persistence and shared/config.py)."""
from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import Mock, call

import pytest

from shared.memory.long_term import user_namespace as long_term_namespace
from shared.persistence import memory_deletion

BACKEND = Path(__file__).resolve().parents[2]
# The shared modules that python-deps-layer.ts stageShared() copies into the layer, besides shared/persistence/.
LAYER_SHARED_MODULES = {
    "shared", "shared.config", "shared.runtime", "shared.runtime.identity", "shared.runtime.model_policy",
    "shared.runtime.model_discovery", "shared.schemas", "shared.schemas.cei_ui",
}


def test_delete_checkpoint_deletes_every_event_on_every_page():
    client = Mock()
    client.list_events.side_effect = [
        {"events": [{"eventId": "e1"}, {"eventId": "e2"}], "nextToken": "page2"},
        {"events": [{"eventId": "e3"}]},
    ]
    memory_deletion.delete_checkpoint("memory-1", "session-1", "user-1", client=client)

    base = {"memoryId": "memory-1", "actorId": "user-1", "sessionId": "session-1",
            "maxResults": 100, "includePayloads": False}
    assert client.list_events.call_args_list == [call(**base), call(**base, nextToken="page2")]
    assert client.delete_event.call_args_list == [
        call(memoryId="memory-1", actorId="user-1", sessionId="session-1", eventId=event_id)
        for event_id in ("e1", "e2", "e3")
    ]


def test_delete_checkpoint_is_a_no_op_when_empty():
    client = Mock()
    client.list_events.return_value = {"events": []}
    memory_deletion.delete_checkpoint("memory-1", "session-1", "user-1", client=client)
    client.delete_event.assert_not_called()


def test_delete_long_term_namespace_deletes_every_page_of_records():
    client = Mock()
    client.list_memory_records.side_effect = [
        {"memoryRecordSummaries": [{"memoryRecordId": "r1"}], "nextToken": "page2"},
        {"memoryRecordSummaries": [{"memoryRecordId": "r2"}]},
    ]
    memory_deletion.delete_long_term_namespace("memory-1", "user-1", client=client)

    assert client.list_memory_records.call_args_list == [
        call(memoryId="memory-1", namespace="/users/user-1/", maxResults=100),
        call(memoryId="memory-1", namespace="/users/user-1/", maxResults=100, nextToken="page2"),
    ]
    assert client.batch_delete_memory_records.call_args_list == [
        call(memoryId="memory-1", records=[{"memoryRecordId": "r1"}]),
        call(memoryId="memory-1", records=[{"memoryRecordId": "r2"}]),
    ]


@pytest.mark.parametrize("user_id", ["user-1", "abc_DEF-123", "a" * 100])
def test_namespace_matches_the_long_term_writer(user_id):
    assert memory_deletion.user_namespace(user_id) == long_term_namespace(user_id)


@pytest.mark.parametrize("user_id", ["", "../x", "a/b", "a" * 101, None])
def test_namespace_rejects_unsafe_ids_like_the_long_term_writer(user_id):
    with pytest.raises(ValueError):
        memory_deletion.user_namespace(user_id)
    with pytest.raises(ValueError):
        long_term_namespace(user_id)


def _shared_imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module == "shared":
            names.update(f"shared.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "shared":
            names.add(node.module)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names if alias.name.split(".")[0] == "shared")
    return names


def _module_file(name: str) -> Path | None:
    base = BACKEND / "agents" / Path(*name.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.exists():
            return candidate
    return None


def _import_closure(entry: Path) -> set[str]:
    """Every shared.* module reached from `entry`, following shared.* imports transitively."""
    seen: set[str] = set()
    pending = list(_shared_imports(entry))
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        source = _module_file(name)
        if source is not None:
            pending.extend(_shared_imports(source))
    return seen


def _layer_ships(name: str) -> bool:
    # Mirrors stageShared() in backend/lib/constructs/python-deps-layer.ts.
    return name in LAYER_SHARED_MODULES or name == "shared.persistence" or name.startswith("shared.persistence.")


@pytest.mark.parametrize("entry", [
    BACKEND / "lambdas" / "delete-worker" / "handler.py",
    BACKEND / "lambdas" / "account-delete" / "handler.py",
], ids=lambda p: p.parent.name)
def test_deletion_lambdas_import_only_layer_shipped_shared_modules(entry):
    missing = {name for name in _import_closure(entry) if not _layer_ships(name)}
    assert missing == set(), f"{entry.parent.name} imports shared modules the python-deps layer does not ship"
