"""Scope Bedrock Converse guardContent to user-typed text only (B51/B51a).

get_model() wires a native Converse guardrailConfig onto every agent model call
(shared/utils.py). Per the Converse API, a request with ANY guardContent block
evaluates ONLY those blocks; a request with NONE evaluates the whole message.
Call sites must therefore wrap exactly the text the user typed -- the query,
a clarification/outline/scope correction, revision feedback -- in guard_content
blocks, and leave retrieved evidence, tool results, recalled memory, system
prompts and agent-generated text as plain text so the Guardrail never masks
paper authors, patent assignees, or FDA contacts in that evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional, Union

from langchain_aws.chat_models.bedrock_converse import ChatBedrockConverse, _messages_to_bedrock
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.outputs import ChatGenerationChunk, ChatResult

from shared.runtime.prompt_cache import PromptCacheMixin


class GuardContentRequiredError(RuntimeError):
    """A guarded model call sent no guardContent block and did not opt out."""


@dataclass(frozen=True)
class Guarded:
    """Marks one text segment as user-typed; it becomes a guard_content block."""

    text: str


Segment = Union[str, Guarded]


def content_blocks(*segments: Segment) -> list[dict[str, str]]:
    """Build Converse content blocks from a mix of plain text and `Guarded` segments.

    Empty segments are dropped. Falls back to a single empty text block so the
    message is never sent with zero content blocks (Bedrock rejects that).
    """
    blocks: list[dict[str, str]] = []
    for segment in segments:
        if isinstance(segment, Guarded):
            if segment.text:
                blocks.append({"type": "guard_content", "text": segment.text})
        elif segment:
            blocks.append({"type": "text", "text": segment})
    if not blocks:
        blocks.append({"type": "text", "text": ""})
    return blocks


def user_turn(user_text: str, *, before: str = "", after: str = "") -> list[dict[str, str]]:
    """Convenience wrapper: one guarded user-text span, with optional plain text around it."""
    return content_blocks(before, Guarded(user_text), after)


def guard_human_messages(messages: list[BaseMessage]) -> list[BaseMessage]:
    """Wrap each plain-string HumanMessage's full content in a guard_content block.

    For call sites (for example tool_agent) that forward `state["messages"]`
    straight to the model: every HumanMessage there is text the user typed, so
    its whole content is guarded; every other message (AIMessage, ToolMessage,
    SystemMessage) is left untouched, since it is not user-typed text.
    """
    converted: list[BaseMessage] = []
    for message in messages:
        if isinstance(message, HumanMessage) and isinstance(message.content, str) and message.content:
            converted.append(message.model_copy(update={"content": content_blocks(Guarded(message.content))}))
        else:
            converted.append(message)
    return converted


def _has_guard_content(bedrock_messages: list[dict[str, Any]], system: list[dict[str, Any]]) -> bool:
    for message in bedrock_messages:
        for block in message.get("content") or []:
            if isinstance(block, dict) and "guardContent" in block:
                return True
    for block in system or []:
        if isinstance(block, dict) and "guardContent" in block:
            return True
    return False


class GuardScopedChatBedrockConverse(PromptCacheMixin, ChatBedrockConverse):
    """ADR: subclass checks the converted request, not a manual block count | fail
    closed per B51 so a missing guardContent silently un-guards typed PII instead
    of masking retrieved evidence; opt out per call with get_model(user_text=False).
    ADR: mixes in PromptCacheMixin (F08) | _generate/_stream below call the guard
    check first, then super() reaches the mixin's cachePoint injection, so caching
    never runs before the fail-closed guardContent check sees the real messages.
    """

    user_text_required: bool = True

    def _check_guard_content(self, messages: List[BaseMessage]) -> None:
        if not self.guardrail_config or not self.user_text_required:
            return
        # Pre-convert on a copy of the list: _messages_to_bedrock may reassign
        # entries for cross-provider replay, but only within this local list.
        bedrock_messages, system = _messages_to_bedrock(
            list(messages), self.system, model_id=self._get_base_model()
        )
        if not _has_guard_content(bedrock_messages, system):
            raise GuardContentRequiredError(
                "Guardrail is configured but this request has no guardContent block for "
                "user-typed text. Wrap the user's text with "
                "shared.runtime.guarded_input.user_turn()/content_blocks(), or call "
                "get_model(..., user_text=False) when this call sends no user-typed text."
            )

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        self._check_guard_content(messages)
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    def _stream(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> "ChatGenerationChunk":
        self._check_guard_content(messages)
        return super()._stream(messages, stop=stop, run_manager=run_manager, **kwargs)
