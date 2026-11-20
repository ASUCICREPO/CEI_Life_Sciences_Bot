"""Bedrock model discovery for `GET /models` and model-id validation (F8, MODEL_POLICY.md step 2).

Lists the account's enabled foundation models and inference profiles, resolves a cross-Region or
custom inference profile to the single underlying foundation model it targets, and caches the
result in-process for a short TTL. A profile that targets more than one foundation model, or
whose models cannot be read, is excluded rather than guessed at (fail closed): an opaque profile
name alone cannot be safely classified against the string-pattern blocklist.

Foundation models are further restricted to ones these tool-using (Converse tool use) text chat
agents can actually run: text in/out, streaming, ON_DEMAND or INFERENCE_PROFILE inference, not
LEGACY, and on the provider allowlist below. This excludes embedding, image, video and speech
models even though they are not blocklisted.
"""
from __future__ import annotations

import fnmatch
import time
from dataclasses import dataclass
from typing import Any

from shared.runtime.model_policy import is_blocked

_DEFAULT_TTL_SECONDS = 300.0
_cache: dict[str, tuple[float, list["DiscoveredModel"]]] = {}
_PROFILE_PREFIXES = ("us.", "eu.", "apac.", "global.")

# ADR: restrict discovery to models the agents can Converse with tool use | picker and
# write-time validation must only ever surface models these tool-using agents can run.
# Source: AWS Bedrock "Supported models and model features" (Converse API tool use column),
# checked 2026-11-18. Patterns match the base foundation-model id (fnmatch).
_TOOL_CAPABLE_MODEL_ID_PATTERNS = (
    "anthropic.claude-*",
    "amazon.nova-*",  # nova canvas/reel/sonic are excluded by the TEXT-modality check below
    "meta.llama3-1-*",
    "meta.llama3-2-11b*",
    "meta.llama3-2-90b*",
    "meta.llama3-3-*",
    "meta.llama4-*",
    "mistral.mistral-large-*",
    "mistral.pixtral-large-*",
    "cohere.command-r*",
    "ai21.jamba-*",
    "deepseek.*",
    "writer.palmyra-x*",
    "qwen.*",
    "openai.gpt-oss-*",
    "google.gemma-*-it",  # "-it" (instruction-tuned) only; "-pt" (pretrained) variants have no tool use
    "moonshotai.kimi-*",
    "moonshot.kimi-*",  # Bedrock has listed Kimi under both "moonshotai." and "moonshot." id prefixes
    "zai.glm-*",
    "xai.grok-*",
)


def _is_tool_capable(summary: dict[str, Any], model_id: str) -> bool:
    """A foundation-model summary describes a model these tool-using agents can run."""
    output_modalities = summary.get("outputModalities")
    input_modalities = summary.get("inputModalities")
    if not isinstance(output_modalities, list) or "TEXT" not in output_modalities:
        return False
    if not isinstance(input_modalities, list) or "TEXT" not in input_modalities:
        return False
    if summary.get("responseStreamingSupported") is not True:
        return False
    inference_types = summary.get("inferenceTypesSupported")
    if not isinstance(inference_types, list) or not any(
        t in inference_types for t in ("ON_DEMAND", "INFERENCE_PROFILE")
    ):
        return False
    lifecycle = summary.get("modelLifecycle")
    if isinstance(lifecycle, dict) and lifecycle.get("status") == "LEGACY":
        return False
    # ADR: strip a region-profile prefix before allowlist matching | mirrors model_policy's base-id
    # matching; real ListFoundationModels ids never carry one, but this keeps both consistent.
    base_id = model_id
    for prefix in _PROFILE_PREFIXES:
        if base_id.startswith(prefix):
            base_id = base_id[len(prefix):]
            break
    return any(fnmatch.fnmatchcase(base_id, pattern) for pattern in _TOOL_CAPABLE_MODEL_ID_PATTERNS)


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
        if isinstance(model_id, str) and model_id and _is_tool_capable(summary, model_id):
            labels[model_id] = summary.get("modelName") or model_id
    return labels


def _profile_underlying_model(summary: dict[str, Any]) -> str | None:
    """The one foundation model a profile targets, if exactly one.

    A cross-Region profile lists one model ARN per Region it routes to (for example us-east-1,
    us-east-2 and us-west-2), all for the same foundation model. Resolve it when every ARN names the
    same model id; a profile whose ARNs name different models stays unresolved (fail closed).
    """
    # ADR: accept one model id across many Region ARNs | Rationale: real SYSTEM_DEFINED us./global.
    # profiles list a model ARN per Region; "exactly one ARN" rejected every cross-Region profile
    # (checked live 2026-11-18) | Alternative: first ARN only rejected -- would hide a mixed-model profile
    models = summary.get("models")
    if not isinstance(models, list) or not models:
        return None
    ids = set()
    for entry in models:
        arn = entry.get("modelArn") if isinstance(entry, dict) else None
        if not isinstance(arn, str) or "/" not in arn:
            return None
        ids.add(arn.rsplit("/", 1)[-1])
    return ids.pop() if len(ids) == 1 else None


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


# ADR: the picker lists only Global and US inference profiles | Rationale: the deploy tool (deploy/cei_deploy)
# lets the admin choose only global. and us. profiles, and blocks an unchosen profile by its prefixed id (B113),
# which leaves the bare foundation-model id unblocked; listing bare ids showed models the admin never chose |
# Alternative: block bare ids in the deploy tool (rejected: an unprefixed pattern blocks every prefix, B113).
_SELECTABLE_PREFIXES = ("global.", "us.")


def _selectable(model_id: str, blocklist: list[str]) -> bool:
    return model_id.startswith(_SELECTABLE_PREFIXES) and not is_blocked(model_id, blocklist)


def permitted_models(client: Any, blocklist: list[str], **kwargs: Any) -> list[DiscoveredModel]:
    """Discovered Global/US profiles, minus the blocklist -- the exact list `GET /models` returns."""
    return [m for m in discover_models_cached(client, **kwargs) if _selectable(m.model_id, blocklist)]


def is_model_available(model_id: str, client: Any, blocklist: list[str], **kwargs: Any) -> bool:
    """Fail-closed check used before a model_id is saved (session create, PUT /settings)."""
    if not isinstance(model_id, str) or not _selectable(model_id, blocklist):
        return False
    return any(m.model_id == model_id for m in discover_models_cached(client, **kwargs))
