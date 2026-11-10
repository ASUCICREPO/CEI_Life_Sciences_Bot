from __future__ import annotations

from typing import Any

from botocore.config import Config
from langchain_aws import ChatBedrockConverse
from langchain_core.messages import HumanMessage

from shared.config import config
from shared.runtime.guarded_input import GuardScopedChatBedrockConverse
from shared.state import BaseAgentState


_BOTO_CONFIG = Config(
    read_timeout=config.bedrock_read_timeout,
    retries={"max_attempts": config.bedrock_max_attempts, "mode": "adaptive"},
)


def get_model(
    model_id: str | None = None, max_tokens: int | None = None, *, user_text: bool = True
) -> ChatBedrockConverse:
    """Build the shared Bedrock Converse model.

    ``user_text`` declares whether this call's messages carry text the user
    typed (the default). When a Guardrail is configured, a call that declares
    ``user_text=True`` but sends no guardContent block fails closed instead of
    silently letting the Guardrail mask all of a tool-result/evidence turn
    (B51/B51a: only user-typed text is scoped for masking). Pass
    ``user_text=False`` only for calls that send no user-typed text at all
    (for example pure synthesis over already-gathered evidence).
    """
    kwargs: dict[str, Any] = {
        "model_id": model_id or config.default_model_id,
        "region_name": config.aws_region,
        "config": _BOTO_CONFIG,
    }
    if config.aws_profile:
        # Local dev only; deployed runtimes leave it unset and use the execution role via the default chain
        kwargs["credentials_profile_name"] = config.aws_profile
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    # ADR: native Converse guardrailConfig, not a separate ApplyGuardrail call | Rationale: B51 requires every
    # agent model call to pass through the Guardrail; wiring it here covers every existing get_model() call site
    # with no change to agent nodes.py/*_main.py. B7: trace stays "disabled" so the Guardrail's own assessment
    # never reaches the agent trace. B51a: output stays unfiltered because every entity in ModelGuardrail
    # (backend/lib/constructs/model-guardrail.ts) is configured outputEnabled=false; only input is assessed.
    # Empty id/version (local dev, no deploy config yet) leaves get_model() unchanged from before this phase.
    model_cls = ChatBedrockConverse
    if config.guardrail_id and config.guardrail_version:
        kwargs["guardrails"] = {
            "guardrail_identifier": config.guardrail_id,
            "guardrail_version": config.guardrail_version,
            "trace": "disabled",
        }
        # ADR: GuardScopedChatBedrockConverse fails closed on a guarded call with no
        # guardContent | B51a scopes masking to user-typed text, not retrieved evidence
        model_cls = GuardScopedChatBedrockConverse
        kwargs["user_text_required"] = user_text
    return model_cls(**kwargs)


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
