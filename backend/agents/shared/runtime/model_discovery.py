"""Bedrock model discovery for `GET /models` and model-id validation (F8, MODEL_POLICY.md step 2).

Lists the account's enabled foundation models and inference profiles, resolves a cross-Region or
custom inference profile to the single underlying foundation model it targets, and caches the
result in-process for a short TTL. A profile that targets more than one foundation model, or
whose models cannot be read, is excluded rather than guessed at (fail closed): an opaque profile
name alone cannot be safely classified against the string-pattern blocklist.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from shared.runtime.model_policy import is_blocked

_DEFAULT_TTL_SECONDS = 300.0
_cache: dict[str, tuple[float, list["DiscoveredModel"]]] = {}


@dataclass(frozen=True)
class DiscoveredModel:
    model_id: str
    label: str


def _foundation_model_labels(client: Any) -> dict[str, str]:
    response = client.list_foundation_models()
    summaries = response.get("modelSummaries") if isinstance(response, dict) else None
    labels: dict[str, str] = {}
    for summary in summaries or []:
        if not isinstance(summary, dict):
            continue
        model_id = summary.get("modelId")
        if isinstance(model_id, str) and model_id:
            labels[model_id] = summary.get("modelName") or model_id
    return labels


def _profile_underlying_model(summary: dict[str, Any]) -> str | None:
    """The one foundation model a profile summary already names, if exactly one."""
    models = summary.get("models")
    if not isinstance(models, list) or len(models) != 1 or not isinstance(models[0], dict):
        return None
    arn = models[0].get("modelArn")
    return arn.rsplit("/", 1)[-1] if isinstance(arn, str) and "/" in arn else None


def _resolve_profile(client: Any, profile_id: str) -> str | None:
    """Fall back to GetInferenceProfile when the list summary did not already resolve the profile."""
    try:
        detail = client.get_inference_profile(inferenceProfileIdentifier=profile_id)
    except Exception:
        return None
    if not isinstance(detail, dict):
        return None
    return _profile_underlying_model(detail)


def discover_models(client: Any) -> list[DiscoveredModel]:
    """Enabled foundation models plus inference profiles resolved to a known underlying model."""
    foundation = _foundation_model_labels(client)
    discovered = {model_id: DiscoveredModel(model_id, label) for model_id, label in foundation.items()}
    response = client.list_inference_profiles()
    summaries = response.get("inferenceProfileSummaries") if isinstance(response, dict) else None
    for summary in summaries or []:
        if not isinstance(summary, dict):
            continue
        profile_id = summary.get("inferenceProfileId")
        if not isinstance(profile_id, str) or not profile_id:
            continue
        underlying = _profile_underlying_model(summary) or _resolve_profile(client, profile_id)
        if underlying is None or underlying not in foundation:
            continue  # opaque, multi-model, or unresolvable profile: excluded, fail closed
        discovered[profile_id] = DiscoveredModel(profile_id, summary.get("inferenceProfileName") or profile_id)
    return list(discovered.values())


def discover_models_cached(client: Any, *, cache_key: str = "default",
                           ttl_seconds: float = _DEFAULT_TTL_SECONDS) -> list[DiscoveredModel]:
    """`discover_models`, cached in-process (per warm Lambda/runtime execution environment)."""
    now = time.monotonic()
    cached = _cache.get(cache_key)
    if cached is not None and now - cached[0] < ttl_seconds:
        return cached[1]
    models = discover_models(client)
    _cache[cache_key] = (now, models)
    return models


def permitted_models(client: Any, blocklist: list[str], **kwargs: Any) -> list[DiscoveredModel]:
    """Discovered models, minus the blocklist — the exact list `GET /models` returns."""
    return [m for m in discover_models_cached(client, **kwargs) if not is_blocked(m.model_id, blocklist)]


def is_model_available(model_id: str, client: Any, blocklist: list[str], **kwargs: Any) -> bool:
    """Fail-closed check used before a model_id is saved (session create, PUT /settings)."""
    if is_blocked(model_id, blocklist):
        return False
    return any(m.model_id == model_id for m in discover_models_cached(client, **kwargs))
