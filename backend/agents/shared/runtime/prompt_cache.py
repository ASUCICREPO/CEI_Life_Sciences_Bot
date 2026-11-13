"""Bedrock Converse prompt caching (``cachePoint``), centralized for every get_model() call site (F08).

Adds a ``{"cachePoint": {"type": "default"}}`` block right after the static system
prompt content and, separately, right after the bound tool definitions -- never
inside/before a ``guardContent`` block and never on a user/evidence message. Gated
per call by the resolved model id so a model that rejects ``cachePoint`` (a
ValidationException) never receives one (fail safe for an unknown model, since
F08 lets a user pick any non-blocklisted Bedrock model).
"""
from __future__ import annotations

import fnmatch
import os
from typing import Any

from langchain_aws.chat_models.bedrock_converse import _is_cache_point
from langchain_core.messages import BaseMessage, SystemMessage

from shared.runtime.model_policy import _model_base

_CACHE_POINT_BLOCK: dict[str, Any] = {"cachePoint": {"type": "default"}}

# ADR: fnmatch tuple of base model ids (post _model_base, lowercased) confirmed by AWS Bedrock's
# "Supported models and Regions for prompt caching" doc as of 2026-11-10 | only these accept
# cachePoint; everything else (Llama, Mistral, Titan, unreleased/unknown ids, etc.) is left alone.
_CACHE_SUPPORTED_PATTERNS: tuple[str, ...] = (
    "anthropic.claude-3-5-haiku-*",
    "anthropic.claude-3-7-sonnet-*",
    "anthropic.claude-opus-4*",
    "anthropic.claude-sonnet-4*",
    "anthropic.claude-haiku-4*",
    "anthropic.claude-opus-5*",
    "anthropic.claude-sonnet-5*",
    "anthropic.claude-haiku-5*",
    "amazon.nova-micro-*",
    "amazon.nova-lite-*",
    "amazon.nova-pro-*",
    "amazon.nova-premier-*",
)


def _cache_enabled() -> bool:
    # ADR: module-level env flag, not shared/config.py | another workstream owns config.py this phase
    raw = os.environ.get("CEI_PROMPT_CACHE_ENABLED", "1")
    return raw.strip().lower() not in ("0", "false", "no", "off")


def supports_cache_point(model_id: str) -> bool:
    """True when the resolved base model id is a Bedrock Converse prompt-caching model."""
    if not isinstance(model_id, str) or not model_id.strip():
        return False
    base = _model_base(model_id).lower()
    return any(fnmatch.fnmatchcase(base, pattern) for pattern in _CACHE_SUPPORTED_PATTERNS)


def _inject_system_cache_point(
    messages: list[BaseMessage], system_field: list[Any] | None
) -> list[BaseMessage]:
    """Insert a cachePoint SystemMessage right after the last static system content.

    Looks at the live ``messages`` list (where every current call site puts its
    system prompt, as a ``{"role": "system", ...}`` entry) as well as the model's
    ``system`` field (unused today, kept for a future call site). No system
    content at all -> nothing to cache, so no cachePoint is added.
    """
    last_system_idx: int | None = None
    for idx, message in enumerate(messages):
        if isinstance(message, SystemMessage):
            last_system_idx = idx
    if last_system_idx is None and not system_field:
        return messages
    new_messages = list(messages)
    insert_at = last_system_idx + 1 if last_system_idx is not None else 0
    new_messages.insert(insert_at, SystemMessage(content=[dict(_CACHE_POINT_BLOCK)]))
    return new_messages


def _inject_tool_cache_point(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Append a cachePoint to bound tool definitions (``tools=`` or ``toolConfig.tools``)."""
    tools = kwargs.get("tools")
    if isinstance(tools, list) and tools and not any(_is_cache_point(t) for t in tools):
        kwargs = dict(kwargs)
        kwargs["tools"] = [*tools, dict(_CACHE_POINT_BLOCK)]
        return kwargs
    tool_config = kwargs.get("toolConfig")
    if isinstance(tool_config, dict):
        tc_tools = tool_config.get("tools")
        if isinstance(tc_tools, list) and tc_tools and not any(_is_cache_point(t) for t in tc_tools):
            kwargs = dict(kwargs)
            kwargs["toolConfig"] = {**tool_config, "tools": [*tc_tools, dict(_CACHE_POINT_BLOCK)]}
    return kwargs


class PromptCacheMixin:
    """Mixin for a ChatBedrockConverse subclass: injects cachePoint before the real call.

    Mix in BEFORE ``ChatBedrockConverse`` (and, for a subclass that also checks
    guardContent like ``GuardScopedChatBedrockConverse``, AFTER that subclass's own
    ``_generate``/``_stream`` override) so the guard check always runs on the
    untouched messages first, per B51/B51a.
    """

    def _with_cache_points(self, messages: list[BaseMessage], kwargs: dict[str, Any]) -> tuple[list[BaseMessage], dict[str, Any]]:
        if not _cache_enabled():
            return messages, kwargs
        try:
            base_model = self._get_base_model()  # type: ignore[attr-defined]
        except Exception:
            return messages, kwargs
        if not supports_cache_point(base_model):
            return messages, kwargs
        system_field = getattr(self, "system", None)
        return _inject_system_cache_point(messages, system_field), _inject_tool_cache_point(kwargs)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
        messages, kwargs = self._with_cache_points(messages, kwargs)
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)  # type: ignore[misc]

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
        messages, kwargs = self._with_cache_points(messages, kwargs)
        return super()._stream(messages, stop=stop, run_manager=run_manager, **kwargs)  # type: ignore[misc]


_wrapped_classes: dict[type, type] = {}


def with_prompt_cache(base_cls: type) -> type:
    """Return a ``PromptCacheMixin`` subclass of ``base_cls``, memoized per class.

    ADR: dynamic wrap (not a static subclass) so a monkeypatched ``get_model()``
    base class (see test_model_credentials.py's ``_FakeModel`` fixture) stays the
    class actually used -- the wrap just adds the two overridden methods on top
    of whatever class is bound at call time.
    """
    wrapped = _wrapped_classes.get(base_cls)
    if wrapped is None:
        wrapped = type(f"PromptCached{base_cls.__name__}", (PromptCacheMixin, base_cls), {})
        _wrapped_classes[base_cls] = wrapped
    return wrapped
