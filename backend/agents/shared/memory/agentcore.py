from __future__ import annotations

from langgraph_checkpoint_aws import AgentCoreMemorySaver, AgentCoreMemoryStore

from shared.config import config


def create_checkpointer(memory_id: str) -> AgentCoreMemorySaver:
    return AgentCoreMemorySaver(memory_id, region_name=config.aws_region)


def create_memory_store(memory_id: str) -> AgentCoreMemoryStore:
    return AgentCoreMemoryStore(memory_id, region_name=config.aws_region)


def build_invocation_config(thread_id: str, actor_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id, "actor_id": actor_id}}
