from __future__ import annotations

from typing import Any

from botocore.config import Config
from langchain_aws import ChatBedrockConverse
from langchain_core.messages import HumanMessage

from shared.config import config
from shared.state import BaseAgentState


_BOTO_CONFIG = Config(
    read_timeout=config.bedrock_read_timeout,
    retries={"max_attempts": config.bedrock_max_attempts, "mode": "adaptive"},
)


def get_model(model_id: str | None = None) -> ChatBedrockConverse:
    return ChatBedrockConverse(
        model_id=model_id or config.default_model_id,
        region_name=config.aws_region,
        credentials_profile_name=config.aws_profile,
        config=_BOTO_CONFIG,
    )


def extract_tokens(response: Any) -> tuple[int, int]:
    usage = getattr(response, "usage_metadata", None)
    if usage:
        return usage.get("input_tokens", 0), usage.get("output_tokens", 0)
    return 0, 0


def last_human_message(state: BaseAgentState) -> str:
    for msg in reversed(state["messages"]):
        if isinstance(msg, HumanMessage):
            return str(msg.content)
    return ""
