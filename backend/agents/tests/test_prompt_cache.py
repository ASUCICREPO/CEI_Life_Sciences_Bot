"""F08: Bedrock Converse prompt caching (cachePoint), injected centrally by get_model().

Every test here builds the real model class (``with_prompt_cache(ChatBedrockConverse)``
or the real ``GuardScopedChatBedrockConverse``) with a fake bedrock-runtime client, the
same pattern ``tests/test_guard_content_scope.py`` uses -- no AWS call, no mocked
``get_model``, so the assertions exercise the exact request-building path get_model()
returns.
"""
from __future__ import annotations

from typing import Any

import pytest
from langchain_aws import ChatBedrockConverse
from langchain_core.tools import tool

from shared.runtime.guarded_input import GuardContentRequiredError, GuardScopedChatBedrockConverse, user_turn
from shared.runtime.prompt_cache import supports_cache_point, with_prompt_cache

_CACHE_POINT = {"cachePoint": {"type": "default"}}


class _FakeBedrockClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {
            "output": {"message": {"content": [{"text": "ok"}]}},
            "usage": {"inputTokens": 1, "outputTokens": 1},
        }


@tool
def _lookup(query: str) -> str:
    """Look something up."""
    return "result"


def _cached_model(model_id: str, **extra: Any) -> tuple[Any, _FakeBedrockClient]:
    fake_client = _FakeBedrockClient()
    cls = with_prompt_cache(ChatBedrockConverse)
    model = cls(model_id=model_id, region_name="us-east-1", client=fake_client, bedrock_client=fake_client, **extra)
    return model, fake_client


def _guard_scoped_model(model_id: str, **extra: Any) -> tuple[Any, _FakeBedrockClient]:
    fake_client = _FakeBedrockClient()
    model = GuardScopedChatBedrockConverse(
        model_id=model_id,
        region_name="us-east-1",
        client=fake_client,
        bedrock_client=fake_client,
        guardrails={"guardrail_identifier": "gr-1", "guardrail_version": "1", "trace": "disabled"},
        **extra,
    )
    return model, fake_client


# ── supports_cache_point: model-id gating ───────────────────────────────────


@pytest.mark.parametrize(
    "model_id",
    [
        "anthropic.claude-3-5-haiku-20241022-v1:0",
        "us.anthropic.claude-3-7-sonnet-20250219-v1:0",
        "us.anthropic.claude-opus-4-20250514-v1:0",
        "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "us.anthropic.claude-sonnet-5",
        "amazon.nova-micro-v1:0",
        "amazon.nova-lite-v1:0",
        "amazon.nova-pro-v1:0",
        "amazon.nova-premier-v1:0",
    ],
)
def test_supported_model_families_match(model_id: str) -> None:
    assert supports_cache_point(model_id) is True


@pytest.mark.parametrize(
    "model_id",
    [
        "meta.llama3-1-70b-instruct-v1:0",
        "mistral.mistral-large-2407-v1:0",
        "amazon.titan-text-express-v1",
        "cohere.command-r-v1:0",
        "",
        "   ",
    ],
)
def test_unsupported_or_unknown_model_ids_do_not_match(model_id: str) -> None:
    assert supports_cache_point(model_id) is False


def test_non_string_model_id_is_fail_safe() -> None:
    assert supports_cache_point(None) is False  # type: ignore[arg-type]


# ── system cachePoint: plain (non-guarded) path ─────────────────────────────


def test_cache_point_appended_after_system_prompt_for_claude_4_5() -> None:
    model, fake_client = _cached_model("us.anthropic.claude-haiku-4-5-20251001-v1:0")
    model.invoke([{"role": "system", "content": "sys prompt"}, {"role": "user", "content": "hi"}])
    assert fake_client.calls[0]["system"] == [{"text": "sys prompt"}, _CACHE_POINT]


def test_cache_point_appended_after_system_prompt_for_nova() -> None:
    model, fake_client = _cached_model("amazon.nova-pro-v1:0")
    model.invoke([{"role": "system", "content": "nova sys prompt"}, {"role": "user", "content": "hi"}])
    assert fake_client.calls[0]["system"] == [{"text": "nova sys prompt"}, _CACHE_POINT]


@pytest.mark.parametrize("model_id", ["meta.llama3-1-70b-instruct-v1:0", "mistral.mistral-large-2407-v1:0"])
def test_no_cache_point_for_unsupported_models(model_id: str) -> None:
    model, fake_client = _cached_model(model_id)
    model.invoke([{"role": "system", "content": "sys prompt"}, {"role": "user", "content": "hi"}])
    assert fake_client.calls[0]["system"] == [{"text": "sys prompt"}]


def test_no_cache_point_when_no_system_content_at_all() -> None:
    model, fake_client = _cached_model("us.anthropic.claude-haiku-4-5-20251001-v1:0")
    model.invoke([{"role": "user", "content": "hi"}])
    assert fake_client.calls[0]["system"] == []


def test_cache_point_disabled_by_env_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CEI_PROMPT_CACHE_ENABLED", "false")
    model, fake_client = _cached_model("us.anthropic.claude-haiku-4-5-20251001-v1:0")
    model.invoke([{"role": "system", "content": "sys prompt"}, {"role": "user", "content": "hi"}])
    assert fake_client.calls[0]["system"] == [{"text": "sys prompt"}]


# ── tool-definition cachePoint: only when tools are bound ──────────────────


def test_cache_point_appended_after_tool_definitions_when_tools_bound() -> None:
    model, fake_client = _cached_model("us.anthropic.claude-haiku-4-5-20251001-v1:0")
    bound = model.bind_tools([_lookup])
    bound.invoke([{"role": "system", "content": "sys prompt"}, {"role": "user", "content": "hi"}])
    tools = fake_client.calls[0]["toolConfig"]["tools"]
    assert tools[-1] == _CACHE_POINT
    assert tools[0]["toolSpec"]["name"] == "_lookup"


def test_no_tool_cache_point_when_no_tools_bound() -> None:
    model, fake_client = _cached_model("us.anthropic.claude-haiku-4-5-20251001-v1:0")
    model.invoke([{"role": "system", "content": "sys prompt"}, {"role": "user", "content": "hi"}])
    assert "toolConfig" not in fake_client.calls[0]


def test_no_tool_cache_point_for_unsupported_model_even_with_tools_bound() -> None:
    model, fake_client = _cached_model("mistral.mistral-large-2407-v1:0")
    bound = model.bind_tools([_lookup])
    bound.invoke([{"role": "user", "content": "hi"}])
    tools = fake_client.calls[0]["toolConfig"]["tools"]
    assert _CACHE_POINT not in tools


# ── guardContent / fail-closed interaction (GuardScopedChatBedrockConverse) ─


def test_guarded_user_block_is_untouched_and_cache_point_is_separate() -> None:
    model, fake_client = _guard_scoped_model("us.anthropic.claude-haiku-4-5-20251001-v1:0")
    model.invoke([{"role": "system", "content": "sys prompt"}, {"role": "user", "content": user_turn("hello")}])
    call = fake_client.calls[0]
    assert call["system"] == [{"text": "sys prompt"}, _CACHE_POINT]
    # The guardContent block carries only the user's text -- no cachePoint before/inside it.
    assert call["messages"] == [
        {"role": "user", "content": [{"guardContent": {"text": {"text": "hello"}}}]}
    ]


def test_guarded_fail_closed_still_raises_with_no_guard_content() -> None:
    model, fake_client = _guard_scoped_model("us.anthropic.claude-haiku-4-5-20251001-v1:0")
    with pytest.raises(GuardContentRequiredError):
        model.invoke([{"role": "system", "content": "sys prompt"}, {"role": "user", "content": "plain text"}])
    assert fake_client.calls == []  # fails before any Bedrock call, cache injection included


def test_guarded_fail_closed_runs_before_cache_injection_mutates_messages() -> None:
    """The guard check sees the same messages whether or not this model supports caching."""
    model, fake_client = _guard_scoped_model("us.anthropic.claude-haiku-4-5-20251001-v1:0")
    with pytest.raises(GuardContentRequiredError):
        model.invoke([{"role": "user", "content": "plain text, no system prompt either"}])
    assert fake_client.calls == []
