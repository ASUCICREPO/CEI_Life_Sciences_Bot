"""Resolve owner-scoped chat settings before starting a specialist turn.

B18: the user may select any non-blocklisted Bedrock model per chat. Real discovery
(ListFoundationModels/ListInferenceProfiles) is done once, at write time, by the Lambdas
that hold that IAM grant (models-settings-handler, session-metadata-handler — see
shared/runtime/model_discovery.py and MODEL_POLICY.md step 2); this runtime's role does not,
so every turn re-verifies only the blocklist, fail closed, rather than re-running discovery.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from shared.config import config
from shared.runtime.model_policy import ModelPolicyError, is_blocked, select_mode

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TurnConfig:
    mode: str
    model_id: str


def _saved_default_model_id(user_id: str) -> str:
    """Owner-scoped saved default (UserMetadataTable), used only for a chat with no stored model_id.

    This runtime's role holds no Bedrock discovery grant (module docstring), so the saved value is
    re-checked only against the blocklist, same as the stored-model path in `resolve_turn_config`. A
    blocklisted saved default, a missing CEI_USER_METADATA_TABLE env var (local runs, no such table
    configured), or any read error all fall back to the global default without leaking details.
    """
    if not os.environ.get("CEI_USER_METADATA_TABLE", "").strip():
        return config.default_model_id
    try:
        from shared.persistence.user_metadata import UserMetadataRepository

        record = UserMetadataRepository().get(user_id)
    except Exception:
        log.exception("saved default model lookup failed; using global default")
        return config.default_model_id
    saved = record.default_model_id if record is not None else None
    if saved is not None and not is_blocked(saved, config.model_blocklist):
        return saved
    return config.default_model_id


async def resolve_turn_config(recorder: Any, *, user_id: str, session_id: str,
                              initial_mode: str | None) -> TurnConfig:
    metadata = await recorder.session_metadata(user_id=user_id, session_id=session_id)
    # An existing setting wins over payload data. New or legacy sessions use the
    # requested initial mode, falling back to science only when none was supplied.
    if not config.default_model_id:
        raise ModelPolicyError("Model unavailable")
    resolved_default: str | None = None
    for _ in range(3):
        stored_mode = metadata.mode if metadata is not None else None
        mode = select_mode(stored_mode or initial_mode or "science")
        selected = metadata.model_id if metadata is not None else None
        if selected is not None:
            candidate = selected
        else:
            # ADR: resolve once per call, not per retry | Rationale: the user's saved default cannot
            # change mid-request; avoids a redundant GetItem on each retry iteration below
            if resolved_default is None:
                resolved_default = _saved_default_model_id(user_id)
            candidate = resolved_default
        if is_blocked(candidate, config.model_blocklist):
            raise ModelPolicyError("Model unavailable")
        if metadata is None or (stored_mode is not None and selected is not None):
            return TurnConfig(mode=mode, model_id=candidate)
        metadata = await recorder.initialize_turn_settings(
            user_id=user_id, session_id=session_id,
            mode=mode if stored_mode is None else None,
            model_id=candidate if selected is None else None,
        )
    raise ModelPolicyError("Chat settings changed; retry the request")
