"""Bedrock-backed chat-title generator (Claude/ChatGPT-style short titles) for ConversationRecorder."""
from __future__ import annotations

import asyncio
import fnmatch
import logging

from shared.config import config
from shared.persistence.conversation import GENERATED_TITLE_CHARS, TitleGenerator

log = logging.getLogger(__name__)

# D3: models that must never be invoked by the application. Matched against the id with any
# cross-Region inference-profile prefix ("us.", "eu.", "apac.", "global.") removed.
BLOCKED_MODEL_PATTERNS: tuple[str, ...] = ("anthropic.claude-fable-5*", "anthropic.claude-mythos-*")
_PROFILE_PREFIXES = ("us.", "eu.", "apac.", "global.")

_PROMPT = (
    "Write a short title (at most 6 words) for this conversation. Reply with the title only: no quotes, "
    "no trailing punctuation.\n\nUser: {prompt}\n\nAssistant: {answer}"
)
_EXCERPT_CHARS = 2000


def is_blocked_model(model_id: str) -> bool:
    base = model_id
    for prefix in _PROFILE_PREFIXES:
        if base.startswith(prefix):
            base = base[len(prefix):]
            break
    return any(fnmatch.fnmatch(base, pattern) for pattern in BLOCKED_MODEL_PATTERNS)


def bedrock_title_generator(
    model_id: str | None = None, timeout_seconds: float | None = None
) -> TitleGenerator:
    model = model_id or config.title_model_id
    if is_blocked_model(model):
        raise ValueError(f"title model {model!r} is on the D3 blocklist")
    timeout = timeout_seconds if timeout_seconds is not None else config.title_timeout_seconds

    async def generate(prompt: str, answer: str) -> str | None:
        from shared.utils import extract_tokens, get_model

        llm = get_model(model, max_tokens=32)
        message = _PROMPT.format(prompt=prompt[:_EXCERPT_CHARS], answer=answer[:_EXCERPT_CHARS])
        response = await asyncio.wait_for(llm.ainvoke(message), timeout=timeout)
        in_tok, out_tok = extract_tokens(response)
        log.info("chat title generated: tokens in=%s out=%s", in_tok, out_tok)
        content = response.content
        if isinstance(content, list):  # Converse content blocks
            content = "".join(c.get("text", "") for c in content if isinstance(c, dict))
        return str(content)[: GENERATED_TITLE_CHARS * 2] or None

    return generate
