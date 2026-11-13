from unittest.mock import Mock

import pytest

from spikes.hitl_spike import runtime


def test_runtime_creates_isolated_checkpointer_for_each_request(monkeypatch):
    monkeypatch.setenv("CEI_SPIKE_MEMORY_ID", "spike-memory")
    checkpointer, graph = object(), object()
    create = Mock(return_value=checkpointer)
    make_graph = Mock(return_value=graph)
    start = Mock(return_value={"status": "awaiting_input"})
    monkeypatch.setattr(runtime, "create_checkpointer", create)
    monkeypatch.setattr(runtime, "create_graph", make_graph)
    monkeypatch.setattr(runtime, "start", start)
    result = runtime.invoke({"operation": "start", "actor_id": "user1", "thread_id": "thread1",
                             "question": "Choose", "options": ["A", "B"]}, None)
    assert result["status"] == "awaiting_input"
    create.assert_called_once_with("spike-memory")
    make_graph.assert_called_once_with(checkpointer)
    start.assert_called_once_with(graph, "user1", "thread1", "Choose", ["A", "B"])


def test_runtime_requires_memory_and_validates_input(monkeypatch):
    monkeypatch.delenv("CEI_SPIKE_MEMORY_ID", raising=False)
    payload = {"operation": "start", "actor_id": "user1", "thread_id": "thread1",
               "question": "Choose", "options": ["A", "B"]}
    with pytest.raises(RuntimeError):
        runtime.invoke(payload, None)
    with pytest.raises(ValueError):
        runtime.invoke({**payload, "actor_id": "../other"}, None)
