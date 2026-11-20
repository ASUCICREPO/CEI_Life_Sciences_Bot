"""Fail-closed building blocks for model selection and input Guardrails.

These helpers are not yet wired into existing agents. Deployment configuration,
session selection, and every model call site must adopt them together.
"""
from __future__ import annotations

import fnmatch
from collections.abc import Iterable, Mapping
from typing import Any

_PROFILE_PREFIXES = ("us.", "eu.", "apac.", "global.")


class ModelPolicyError(ValueError):
    """A model or mode is not permitted."""


class GuardrailBlocked(Exception):
    """A Guardrail blocked or could not safely assess the user input."""


def _model_base(model_id: str) -> str:
    # ARN resources may contain a foundation model or inference profile ID.
    model_id = model_id.rsplit("/", 1)[-1]
    for prefix in _PROFILE_PREFIXES:
        if model_id.startswith(prefix):
            return model_id[len(prefix):]
    return model_id


# ADR: a prefixed blocklist entry blocks only that profile | Rationale: B113 — per-profile control (for
# example allow us. profiles but block global. for data residency); unprefixed entries, including the D3
# safety patterns, still block the model under every prefix | Alternative: base-id matching for every
# entry (rejected: could not allow us. while blocking global.)
def is_blocked(model_id: str, blocklist: Iterable[str]) -> bool:
    if not isinstance(model_id, str) or not model_id.strip():
        return True
    full = model_id.rsplit("/", 1)[-1]
    base = _model_base(model_id)
    for pattern in blocklist:
        if not isinstance(pattern, str) or not pattern:
            continue
        if pattern.startswith(_PROFILE_PREFIXES):
            if fnmatch.fnmatchcase(full, pattern):
                return True
        elif fnmatch.fnmatchcase(model_id, pattern) or fnmatch.fnmatchcase(base, pattern):
            return True
    return False


def permitted_models(model_ids: Iterable[str], blocklist: Iterable[str]) -> list[str]:
    """Filter server-discovered enabled models using the same rule as invocation."""
    patterns = tuple(blocklist)
    return [model for model in model_ids if isinstance(model, str) and not is_blocked(model, patterns)]


def select_model(requested: str | None, *, user_default: str | None, global_default: str,
                 available: Iterable[str], blocklist: Iterable[str]) -> str:
    """Resolve once per turn; reject invalid selections rather than silently falling back."""
    selected = requested or user_default or global_default
    if not isinstance(selected, str) or selected not in set(available) or is_blocked(selected, blocklist):
        raise ModelPolicyError("Model unavailable")
    return selected


def select_mode(mode: str) -> str:
    if mode not in ("science", "business"):
        raise ModelPolicyError("Mode unavailable")
    return mode


def _actions(value: Any) -> list[str]:
    if isinstance(value, Mapping):
        actions = [value["action"]] if isinstance(value.get("action"), str) else []
        return actions + [action for item in value.values() for action in _actions(item)]
    if isinstance(value, list):
        return [action for item in value for action in _actions(item)]
    return []


def mask_user_input(text: str, *, client: Any, guardrail_id: str, guardrail_version: str) -> str:
    """Apply input Guardrail and return only safe text; never expose assessments.

    `ANONYMIZED` intervention can yield a masked text; any BLOCKED, unknown or
    uncharacterized intervention fails closed rather than sending a canned refusal
    or unmasked input to a model. This does not assess tool results or model output.
    """
    if not isinstance(text, str) or not text or len(text) > 16000:
        raise ValueError("invalid input")
    if not all(isinstance(v, str) and v.strip() for v in (guardrail_id, guardrail_version)):
        raise GuardrailBlocked("Input could not be assessed")
    response = client.apply_guardrail(guardrailIdentifier=guardrail_id, guardrailVersion=guardrail_version,
        source="INPUT", content=[{"text": {"text": text}}])
    if not isinstance(response, Mapping):
        raise GuardrailBlocked("Input could not be assessed")
    if response.get("action") == "NONE":
        return text
    if response.get("action") != "GUARDRAIL_INTERVENED":
        raise GuardrailBlocked("Input could not be assessed")
    actions = _actions(response.get("assessments"))
    if "ANONYMIZED" not in actions or any(action not in ("ANONYMIZED", "NONE") for action in actions):
        raise GuardrailBlocked("Input was blocked")
    outputs = response.get("outputs")
    if not isinstance(outputs, list) or len(outputs) != 1 or not isinstance(outputs[0], Mapping):
        raise GuardrailBlocked("Input could not be assessed")
    masked = outputs[0].get("text")
    if not isinstance(masked, str) or not masked:
        raise GuardrailBlocked("Input could not be assessed")
    return masked
