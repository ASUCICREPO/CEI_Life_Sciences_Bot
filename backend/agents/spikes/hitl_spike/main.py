"""Run each operation in a separate process against an existing dev memory.

Example: python -m spikes.hitl_spike.main start --memory-id ID --actor-id USER --thread-id SESSION
The resume operation reads the typed ActionRequest JSON from stdin.
"""
from __future__ import annotations

import argparse
import json
import sys

from shared.memory.agentcore import create_checkpointer
from spikes.hitl_spike.graph import create_graph, resume, start


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("start", "resume"))
    parser.add_argument("--memory-id", required=True)
    parser.add_argument("--actor-id", required=True)
    parser.add_argument("--thread-id", required=True)
    args = parser.parse_args()
    graph = create_graph(create_checkpointer(args.memory_id))
    if args.operation == "start":
        result = start(graph, args.actor_id, args.thread_id, "Choose an option", ["A", "B"])
    else:
        result = resume(graph, args.actor_id, args.thread_id, json.load(sys.stdin))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
