"""Resolve owner-scoped chat settings before starting a specialist turn.

Model discovery and global-blocklist enforcement are not wired yet. Until that
work is complete, only the configured runtime default is accepted;
reject a different saved model instead of silently using the wrong one.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from shared.config import config
from shared.runtime.model_policy import ModelPolicyError, select_mode


@dataclass(frozen=True)
class TurnConfig:
    mode: str
    model_id: str


async def resolve_turn_config(recorder: Any, *, user_id: str, session_id: str,
                              initial_mode: str | None) -> TurnConfig:
    metadata = await recorder.session_metadata(user_id=user_id, session_id=session_id)
    # An existing setting wins over payload data. New or legacy sessions use the
    # requested initial mode, falling back to science only when none was supplied.
    if not config.default_model_id:
        raise ModelPolicyError("Model unavailable")
    for _ in range(3):
        stored_mode = metadata.mode if metadata is not None else None
        mode = select_mode(stored_mode or initial_mode or "science")
        selected = metadata.model_id if metadata is not None else None
        if selected is not None and selected != config.default_model_id:
            raise ModelPolicyError("Model unavailable")
        if metadata is None or (stored_mode is not None and selected is not None):
            return TurnConfig(mode=mode, model_id=config.default_model_id)
        metadata = await recorder.initialize_turn_settings(
            user_id=user_id, session_id=session_id,
            mode=mode if stored_mode is None else None,
            model_id=config.default_model_id if selected is None else None,
        )
    raise ModelPolicyError("Chat settings changed; retry the request")
