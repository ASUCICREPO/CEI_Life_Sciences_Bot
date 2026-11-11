"""AgentCore Memory deletion for the account-delete cascade (F19/B109), boto3 only.

ADR: boto3-only module in shared/persistence | Rationale: the delete-worker Lambda gets shared code
only from the python-deps layer, which ships shared/persistence and shared/config.py but not
shared/memory or langgraph_checkpoint_aws; importing shared.memory.agentcore there fails at run
time | Alternative: add langgraph_checkpoint_aws and shared/memory to the layer, rejected because
it adds a large dependency to every Lambda that uses the layer.
"""
from __future__ import annotations

import re
from typing import Any

from shared.config import config

# Same rule and namespace format as shared/memory/long_term.py (`_identifier`, `user_namespace`).
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")


def user_namespace(user_id: str) -> str:
    if not isinstance(user_id, str) or _IDENTIFIER.fullmatch(user_id) is None:
        raise ValueError("invalid memory identity")
    return f"/users/{user_id}/"


def _client(client: Any) -> Any:
    if client is not None:
        return client
    import boto3

    return boto3.client("bedrock-agentcore", region_name=config.aws_region)


def delete_checkpoint(memory_id: str, thread_id: str, actor_id: str = "", *, client: Any = None) -> None:
    """Delete every event under (memory_id, actor_id, thread_id): checkpoints and raw conversation events.

    Same calls as langgraph_checkpoint_aws AgentCoreMemorySaver.delete_thread (ListEvents, then
    DeleteEvent for each event). Idempotent: a thread with no events is a no-op.
    """
    agentcore = _client(client)
    params: dict[str, Any] = {
        "memoryId": memory_id, "actorId": actor_id, "sessionId": thread_id,
        "maxResults": 100, "includePayloads": False,
    }
    while True:
        response = agentcore.list_events(**params)
        events = response.get("events", [])
        if not events:
            return
        for event in events:
            agentcore.delete_event(memoryId=memory_id, actorId=actor_id, sessionId=thread_id,
                                   eventId=event["eventId"])
        next_token = response.get("nextToken")
        if not next_token:
            return
        params["nextToken"] = next_token


def delete_long_term_namespace(memory_id: str, user_id: str, *, client: Any = None) -> None:
    """Delete every extracted long-term memory record in the user's namespace (both strategies)."""
    agentcore = _client(client)
    namespace = user_namespace(user_id)
    next_token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"memoryId": memory_id, "namespace": namespace, "maxResults": 100}
        if next_token:
            kwargs["nextToken"] = next_token
        response = agentcore.list_memory_records(**kwargs)
        records = response.get("memoryRecordSummaries", [])
        if records:
            agentcore.batch_delete_memory_records(
                memoryId=memory_id,
                records=[{"memoryRecordId": record["memoryRecordId"]} for record in records],
            )
        next_token = response.get("nextToken")
        if not next_token:
            return
