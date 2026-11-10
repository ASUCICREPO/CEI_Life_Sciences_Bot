"""Mask user-authored LangChain messages before they are sent to a model.

This adapter is deliberately not wired to any runtime until the Guardrail resource,
checkpoint/history paths, and all model calls can be integrated together.
"""
from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage

from shared.runtime.model_policy import GuardrailBlocked, mask_user_input


def _mask_content(content: Any, *, client: Any, guardrail_id: str, guardrail_version: str) -> Any:
    def mask(text: str) -> str:
        if not text:
            return text
        return mask_user_input(text, client=client, guardrail_id=guardrail_id,
                               guardrail_version=guardrail_version)

    if isinstance(content, str):
        return mask(content)
    if not isinstance(content, list):
        raise GuardrailBlocked("User input could not be assessed")
    sanitized = []
    for block in content:
        # Reject unrecognized/multimodal structures rather than leaking raw text.
        # Upload/image support is deferred for this iteration.
        if not isinstance(block, dict) or set(block) != {"type", "text"} or block.get("type") != "text" or not isinstance(block.get("text"), str):
            raise GuardrailBlocked("User input could not be assessed")
        sanitized.append({"type": "text", "text": mask(block["text"])})
    return sanitized


def mask_user_messages(messages: Sequence[BaseMessage], *, client: Any, guardrail_id: str,
                       guardrail_version: str) -> list[BaseMessage]:
    """Mask every user message, including prior history; preserve tool/AI results.

    Output is a new list, with replacement HumanMessage instances. An exception
    fails the entire request; partially assessed messages are never returned. This
    only protects the exact list passed here: user-derived text copied to prompts,
    arbitrary state fields, title generation, resumes, and checkpoints need their
    own coverage before integration can claim all inputs are guarded.
    """
    if not guardrail_id or not guardrail_version:
        raise GuardrailBlocked("User input could not be assessed")
    masked: list[BaseMessage] = []
    for message in messages:
        if not isinstance(message, BaseMessage):
            raise GuardrailBlocked("User input could not be assessed")
        if isinstance(message, HumanMessage):
            if message.additional_kwargs:
                raise GuardrailBlocked("User input could not be assessed")
            content = _mask_content(message.content, client=client, guardrail_id=guardrail_id,
                                    guardrail_version=guardrail_version)
            masked.append(message.model_copy(update={"content": content}))
        else:
            masked.append(message)
    return masked


async def amask_user_messages(messages: Sequence[BaseMessage], *, client: Any, guardrail_id: str,
                              guardrail_version: str) -> list[BaseMessage]:
    """Offload the synchronous Bedrock client for asynchronous agent entrypoints."""
    return await asyncio.to_thread(mask_user_messages, messages, client=client, guardrail_id=guardrail_id,
                                   guardrail_version=guardrail_version)
