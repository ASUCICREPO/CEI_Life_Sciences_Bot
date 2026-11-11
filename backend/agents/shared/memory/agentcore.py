from __future__ import annotations

from typing import Any

from langgraph_checkpoint_aws import AgentCoreMemorySaver, AgentCoreMemoryStore

from shared.config import config


def create_checkpointer(memory_id: str) -> AgentCoreMemorySaver:
    return AgentCoreMemorySaver(memory_id, region_name=config.aws_region)


def create_memory_store(memory_id: str) -> AgentCoreMemoryStore:
    return AgentCoreMemoryStore(memory_id, region_name=config.aws_region)


def build_invocation_config(thread_id: str, actor_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id, "actor_id": actor_id}}


def delete_checkpoint(memory_id: str, thread_id: str, actor_id: str = "") -> None:
    """Idempotently remove a thread's persistent checkpoint (B109/F19 account-delete cascade).

    `AgentCoreMemorySaver.delete_thread` lists and deletes every AgentCore Memory event under
    (memory_id, actor_id, thread_id) -- the same (memoryId, actorId, sessionId) triple that
    `shared.memory.long_term.record_conversation` writes conversational events under, so this
    also removes a session's raw long-term-memory source events, not only its checkpoint blobs.
    A no-op (ListEvents returns nothing) when the thread never had a persistent checkpoint.
    """
    create_checkpointer(memory_id).delete_thread(thread_id, actor_id)


def delete_long_term_namespace(memory_id: str, user_id: str, *, client: Any = None) -> None:
    """Remove every extracted long-term memory record (B34/B37 user_preferences + semantic_facts)
    in a user's namespace. Raw source events are removed per-session by `delete_checkpoint` above;
    this only clears the strategies' derived records.
    """
    import boto3

    from shared.memory.long_term import user_namespace

    agentcore = client or boto3.client("bedrock-agentcore", region_name=config.aws_region)
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
