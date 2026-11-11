"""AgentCore async spike. Uses a dedicated DynamoDB cancellation table.

This is a development experiment, not a production job API. The table must have
partition key ``job_id`` (String); setting ``cancel_requested`` to true requests
cooperative cancellation. CEI_SPIKE_MEMORY_ID and CEI_SPIKE_CANCEL_TABLE must
name existing isolated development resources.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re

import boto3
from bedrock_agentcore.runtime import BedrockAgentCoreApp

from shared.memory.agentcore import build_invocation_config, create_checkpointer
from spikes.async_spike.graph import create_graph

app = BedrockAgentCoreApp()
log = logging.getLogger(__name__)
tasks: set[asyncio.Task] = set()
_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


@app.entrypoint
async def invoke(payload, context):
    job_id = payload["job_id"]
    actor_id = payload["actor_id"]
    thread_id = payload["thread_id"]
    if not all(isinstance(value, str) and _ID.fullmatch(value) for value in (job_id, actor_id, thread_id)):
        raise ValueError("invalid spike identifier")
    total = int(payload.get("total_steps", 66))
    delay = float(payload.get("delay_seconds", 60))
    if not 1 <= total <= 10000 or not 0 <= delay <= 600:
        raise ValueError("invalid spike duration")
    table = boto3.resource("dynamodb").Table(os.environ["CEI_SPIKE_CANCEL_TABLE"])

    async def is_cancelled():
        result = await asyncio.to_thread(table.get_item, Key={"job_id": job_id}, ConsistentRead=True)
        return bool(result.get("Item", {}).get("cancel_requested", False))

    graph = create_graph(create_checkpointer(os.environ["CEI_SPIKE_MEMORY_ID"]), is_cancelled)
    config = build_invocation_config(thread_id, actor_id)
    config["recursion_limit"] = total + 10
    task_id = app.add_async_task("async-spike", {"job_id": job_id})

    async def run():
        try:
            initial = None if payload.get("resume") is True else {
                "completed_steps": 0, "total_steps": total, "delay_seconds": delay, "cancelled": False,
            }
            result = await graph.ainvoke(initial, config)
            log.info("async spike ended job=%s completed=%s cancelled=%s", job_id, result["completed_steps"], result["cancelled"])
        except Exception:
            log.exception("async spike failed job=%s", job_id)
        finally:
            app.complete_async_task(task_id)

    task = asyncio.create_task(run())
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return {"status": "running", "job_id": job_id}


if __name__ == "__main__":
    app.run()
