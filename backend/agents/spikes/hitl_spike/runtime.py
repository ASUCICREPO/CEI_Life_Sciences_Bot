"""Isolated AgentCore runtime entry point for the S4 pause/resume proof.

This experimental endpoint accepts only synthetic identifiers and data. It is
not a production HITL API, and must be deployed with an isolated memory resource
and IAM-restricted invocation permissions.
"""
from __future__ import annotations

import os
import re
from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from shared.memory.agentcore import create_checkpointer
from spikes.hitl_spike.graph import create_graph, resume, start

app = BedrockAgentCoreApp()
_ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")


@app.entrypoint
def invoke(payload: Any, _context: Any) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("invalid spike request")
    actor, thread, operation = payload.get("actor_id"), payload.get("thread_id"), payload.get("operation")
    if not all(isinstance(value, str) and _ID.fullmatch(value) for value in (actor, thread)):
        raise ValueError("invalid spike identifiers")
    if operation not in ("start", "resume"):
        raise ValueError("invalid spike operation")
    memory_id = os.environ.get("CEI_SPIKE_MEMORY_ID", "").strip()
    if not memory_id:
        raise RuntimeError("isolated spike memory is required")
    graph = create_graph(create_checkpointer(memory_id))
    if operation == "start":
        question, options = payload.get("question"), payload.get("options")
        if (not isinstance(question, str) or not 1 <= len(question) <= 500
                or not isinstance(options, list) or not 2 <= len(options) <= 8
                or any(not isinstance(option, str) or not 1 <= len(option) <= 100 for option in options)
                or len(set(options)) != len(options)):
            raise ValueError("invalid spike question")
        return start(graph, actor, thread, question, options)
    action = payload.get("action")
    if not isinstance(action, dict):
        raise ValueError("invalid spike action")
    return resume(graph, actor, thread, action)


if __name__ == "__main__":
    app.run()
