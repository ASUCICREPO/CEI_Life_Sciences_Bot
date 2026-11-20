"""Bedrock-backed chat-title generator (Claude/ChatGPT-style short titles) for ConversationRecorder."""
from __future__ import annotations

import asyncio
import logging

from shared.config import config
from shared.persistence.conversation import GENERATED_TITLE_CHARS, TitleGenerator
from shared.runtime.guarded_input import user_turn
from shared.runtime.model_policy import is_blocked

log = logging.getLogger(__name__)

_PROMPT_HEADER = (
    "Write a short title (at most 6 words) for this conversation. Reply with the title only: no quotes, "
    "no trailing punctuation.\n\nUser: "
)
_EXCERPT_CHARS = 2000


def is_blocked_model(model_id: str) -> bool:
    # ADR: config.model_blocklist (D3 + CEI_BLOCKED_MODELS) | Rationale: B18 — one blocklist, one matching rule
    # (shared/runtime/model_policy.is_blocked), shared with /models, session create, and the per-turn recheck.
    return is_blocked(model_id, config.model_blocklist)


def bedrock_title_generator(
    model_id: str | None = None, timeout_seconds: float | None = None
) -> TitleGenerator:
    # ADR: titles use the deployed default model | Rationale: a hard-coded title model could be blocked by the
    # deploy-time blocklist and fail every turn | Alternative: a separate CEI_TITLE_MODEL_ID (rejected: one more
    # model to keep allowed). A blocked model only skips the title; it never stops the turn.
    model = model_id or config.default_model_id
    if is_blocked_model(model):
        log.warning("chat title skipped: title model is on the blocklist")

        async def skip(prompt: str, answer: str) -> str | None:
            return None

        return skip
    timeout = timeout_seconds if timeout_seconds is not None else config.title_timeout_seconds

    async def generate(prompt: str, answer: str) -> str | None:
        from shared.utils import extract_tokens, get_model

        llm = get_model(model, max_tokens=32)
        # ADR: prompt is user-typed, the assistant's own answer is not | B51a scope
        message = user_turn(
            prompt[:_EXCERPT_CHARS], before=_PROMPT_HEADER, after=f"\n\nAssistant: {answer[:_EXCERPT_CHARS]}"
        )
        response = await asyncio.wait_for(llm.ainvoke([{"role": "user", "content": message}]), timeout=timeout)
        in_tok, out_tok = extract_tokens(response)
        log.info("chat title generated: tokens in=%s out=%s", in_tok, out_tok)
        content = response.content
        if isinstance(content, list):  # Converse content blocks
            content = "".join(c.get("text", "") for c in content if isinstance(c, dict))
        return str(content)[: GENERATED_TITLE_CHARS * 2] or None

    return generate
