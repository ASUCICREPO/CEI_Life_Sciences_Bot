"""Bedrock model discovery for the Models screen.

Two layers, deliberately different in scope:

- "backend-discoverable" models: every model `backend/agents/shared/runtime/model_discovery.py`
  would surface to the running app (any provider its tool-capable patterns match, D3-blocked or
  not). This tool never shows most of these in the picker, but still needs the full set to compute
  `blockedModelIds` (B18): anything backend-discoverable that the operator did not explicitly keep
  gets written to the blocklist, so the deployed app only ever offers the providers this TUI shows.
- "picker" models: the backend-discoverable set narrowed to the provider allowlist below, resolved
  through a `global.`/`us.` inference profile (B Model picker only offers profiles, not raw
  foundation-model ids, matching how `shared/runtime/config.py` / `shared/utils.get_model()` select
  a model).

`_TOOL_CAPABLE_MODEL_ID_PATTERNS` and `_D3_BLOCKED_MODEL_PATTERNS` below are intentionally exact
copies of the backend constants; `deploy/tests/test_drift.py` parses both backend files with `ast`
and fails if they ever diverge.
"""
from __future__ import annotations

import fnmatch
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

_PROFILE_PREFIXES = ("us.", "eu.", "apac.", "global.")

# ADR: exact copy of backend/agents/shared/runtime/model_discovery._TOOL_CAPABLE_MODEL_ID_PATTERNS |
# Rationale: this tool must offer exactly the providers/models the deployed backend can discover;
# a drift test (deploy/tests/test_drift.py) keeps the two lists honest
_TOOL_CAPABLE_MODEL_ID_PATTERNS = (
    "anthropic.claude-*",
    "amazon.nova-*",
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
    "google.gemma-*-it",
    "moonshotai.kimi-*",
    "moonshot.kimi-*",
    "zai.glm-*",
    "xai.grok-*",
)

# ADR: exact copy of backend/agents/shared/config.D3_BLOCKED_MODEL_PATTERNS | see test_drift.py
_D3_BLOCKED_MODEL_PATTERNS = ("anthropic.claude-fable-5*", "anthropic.claude-mythos-*")

# The providers this deploy tool lets an operator pick from (a strict subset of everything
# backend-discoverable; everything else ends up in blockedModelIds automatically, see module doc).
PROVIDER_ALLOWLIST_RAW = ("OpenAI", "Anthropic", "Qwen", "Google", "Meta", "Moonshot", "Z.AI", "xAI")
PROVIDER_ID_PREFIXES = (
    "openai.", "anthropic.", "qwen.", "google.", "meta.", "moonshotai.", "moonshot.", "zai.", "xai.",
)

_NUM_RE = re.compile(r"\d+")


def _normalize_provider(name: str) -> str:
    s = (name or "").lower().replace(" ", "").replace(".", "")
    if len(s) > 2 and s.endswith("ai"):
        s = s[:-2]
    return s


_PROVIDER_ALLOWLIST = {_normalize_provider(p) for p in PROVIDER_ALLOWLIST_RAW}


def strip_profile_prefix(model_id: str) -> str:
    for prefix in _PROFILE_PREFIXES:
        if model_id.startswith(prefix):
            return model_id[len(prefix):]
    return model_id


def is_blocked(model_id: str, blocklist: Sequence[str]) -> bool:
    """Copy of shared/runtime/model_policy.is_blocked (base-id fnmatch, prefix-stripped)."""
    if not isinstance(model_id, str) or not model_id.strip():
        return True
    base = strip_profile_prefix(model_id)
    for pattern in blocklist:
        if not isinstance(pattern, str) or not pattern:
            continue
        if fnmatch.fnmatchcase(model_id, pattern) or fnmatch.fnmatchcase(base, strip_profile_prefix(pattern)):
            return True
    return False


def _passes_modality(summary: Dict[str, Any]) -> bool:
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
    return True


def is_multimodal(summary: Dict[str, Any]) -> bool:
    """Text and image input (the picker lists only multimodal models)."""
    input_modalities = summary.get("inputModalities")
    return isinstance(input_modalities, list) and "IMAGE" in input_modalities


def _matches_tool_capable_pattern(base_id: str) -> bool:
    return any(fnmatch.fnmatchcase(base_id, pattern) for pattern in _TOOL_CAPABLE_MODEL_ID_PATTERNS)


def is_backend_discoverable(summary: Dict[str, Any]) -> bool:
    """Would backend/agents/shared/runtime/model_discovery.py surface this foundation model?"""
    model_id = summary.get("modelId")
    if not isinstance(model_id, str) or not model_id:
        return False
    if not _passes_modality(summary):
        return False
    return _matches_tool_capable_pattern(strip_profile_prefix(model_id))


def is_provider_allowed(summary: Dict[str, Any], model_id: str) -> bool:
    """Is this model from one of the providers the picker shows (OpenAI/Anthropic/Qwen/.../xAI)?"""
    provider = summary.get("providerName")
    if isinstance(provider, str) and _normalize_provider(provider) in _PROVIDER_ALLOWLIST:
        return True
    base = strip_profile_prefix(model_id)
    return base.startswith(PROVIDER_ID_PREFIXES)


class ModelDiscoveryError(Exception):
    """bedrock:ListFoundationModels / bedrock:ListInferenceProfiles failed."""

    REQUIRED_PERMISSIONS = ("bedrock:ListFoundationModels", "bedrock:ListInferenceProfiles")

    def __init__(self, message: str, cause: Optional[BaseException] = None):
        super().__init__(message)
        self.cause = cause


@dataclass(frozen=True)
class ModelItem:
    profile_id: str
    label: str
    base_model_id: str
    provider: str


@dataclass
class DiscoveryResult:
    global_items: List[ModelItem] = field(default_factory=list)
    us_items: List[ModelItem] = field(default_factory=list)
    all_backend_ids: List[str] = field(default_factory=list)
    default_model_id: Optional[str] = None
    router_model_id: Optional[str] = None

    def items_by_group(self) -> Dict[str, List[ModelItem]]:
        return {"global": self.global_items, "us": self.us_items}

    def all_items(self) -> List[ModelItem]:
        return self.global_items + self.us_items


def _list_foundation_summaries(client: Any) -> List[Dict[str, Any]]:
    response = client.list_foundation_models()
    summaries = response.get("modelSummaries") if isinstance(response, dict) else None
    return [s for s in (summaries or []) if isinstance(s, dict)]


def _list_inference_profile_summaries(client: Any, type_equals: str = "SYSTEM_DEFINED") -> List[Dict[str, Any]]:
    summaries: List[Dict[str, Any]] = []
    next_token = None
    while True:
        kwargs: Dict[str, Any] = {"typeEquals": type_equals, "maxResults": 100}
        if next_token:
            kwargs["nextToken"] = next_token
        response = client.list_inference_profiles(**kwargs)
        page = response.get("inferenceProfileSummaries") if isinstance(response, dict) else None
        summaries.extend(s for s in (page or []) if isinstance(s, dict))
        next_token = response.get("nextToken") if isinstance(response, dict) else None
        if not next_token:
            break
    return summaries


def _profile_underlying_model(summary: Dict[str, Any]) -> Optional[str]:
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


def _resolve_profile_fallback(client: Any, profile_id: str) -> Optional[str]:
    try:
        detail = client.get_inference_profile(inferenceProfileIdentifier=profile_id)
    except Exception:
        return None
    if not isinstance(detail, dict):
        return None
    return _profile_underlying_model(detail)


def _sort_key(item: ModelItem) -> Tuple[Tuple[int, ...], str]:
    nums = tuple(int(n) for n in _NUM_RE.findall(item.base_model_id))
    return (nums, item.label)


def discover(client: Any, *, max_workers: int = 8) -> DiscoveryResult:
    """Run `list_foundation_models`/`list_inference_profiles` and build the picker's data.

    Raises ModelDiscoveryError on any AWS call failure so the caller can fall back to manual input.
    """
    try:
        foundation = _list_foundation_summaries(client)
    except Exception as exc:  # noqa: BLE001 - surfaced to the TUI as a single actionable error
        raise ModelDiscoveryError("Could not list Bedrock foundation models.", exc) from exc

    backend_discoverable: Dict[str, Dict[str, Any]] = {
        s["modelId"]: s for s in foundation if is_backend_discoverable(s)
    }
    all_backend_ids = sorted(backend_discoverable.keys())
    # The picker shows only multimodal models (text + image in); text-only ones stay in all_backend_ids,
    # so they are blocked like any unchecked model.
    allowed_foundation = {
        model_id: s
        for model_id, s in backend_discoverable.items()
        if is_provider_allowed(s, model_id) and is_multimodal(s)
        and not is_blocked(model_id, _D3_BLOCKED_MODEL_PATTERNS)
    }

    try:
        profiles = _list_inference_profile_summaries(client)
    except Exception as exc:  # noqa: BLE001
        raise ModelDiscoveryError("Could not list Bedrock inference profiles.", exc) from exc

    # Profiles whose summary doesn't already resolve to one model need a get_inference_profile call;
    # those run in parallel since there can be many and each is a separate round trip.
    unresolved: List[Dict[str, Any]] = []
    resolved: Dict[str, str] = {}
    for summary in profiles:
        profile_id = summary.get("inferenceProfileId")
        if not isinstance(profile_id, str) or not profile_id:
            continue
        if not (profile_id.startswith("global.") or profile_id.startswith("us.")):
            continue
        underlying = _profile_underlying_model(summary)
        if underlying is not None:
            resolved[profile_id] = underlying
        else:
            unresolved.append(summary)

    if unresolved:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(_resolve_profile_fallback, client, s["inferenceProfileId"]): s
                for s in unresolved
            }
            for future, summary in futures.items():
                underlying = future.result()
                if underlying is not None:
                    resolved[summary["inferenceProfileId"]] = underlying

    global_items: List[ModelItem] = []
    us_items: List[ModelItem] = []
    profile_names = {s["inferenceProfileId"]: s for s in profiles if isinstance(s.get("inferenceProfileId"), str)}
    for profile_id, underlying in resolved.items():
        base_summary = allowed_foundation.get(underlying)
        if base_summary is None:
            continue  # not provider-allowed (or not backend-discoverable): never shown in the picker
        summary = profile_names.get(profile_id, {})
        label = summary.get("inferenceProfileName") or profile_id
        provider = base_summary.get("providerName") or ""
        item = ModelItem(profile_id=profile_id, label=label, base_model_id=underlying, provider=provider)
        if profile_id.startswith("global."):
            global_items.append(item)
        else:
            us_items.append(item)

    global_items.sort(key=_sort_key, reverse=True)
    us_items.sort(key=_sort_key, reverse=True)

    result = DiscoveryResult(global_items=global_items, us_items=us_items, all_backend_ids=all_backend_ids)
    result.default_model_id, result.router_model_id = compute_defaults(result)
    return result


def _find_default(items_by_group: Dict[str, List[ModelItem]], pattern: str) -> Optional[str]:
    for group in ("global", "us"):
        for item in items_by_group.get(group, []):
            if fnmatch.fnmatchcase(item.base_model_id, pattern):
                return item.profile_id
    return None


def compute_defaults(result: DiscoveryResult) -> Tuple[Optional[str], Optional[str]]:
    """Smart defaults: newest Sonnet (default) / newest Haiku (router), preferring `global.`."""
    items_by_group = result.items_by_group()
    fallback: Optional[str] = None
    for group in ("global", "us"):
        if items_by_group.get(group):
            fallback = items_by_group[group][0].profile_id
            break
    default_model_id = _find_default(items_by_group, "*anthropic.claude-sonnet-*") or fallback
    router_model_id = _find_default(items_by_group, "*anthropic.claude-haiku-*") or fallback
    return default_model_id, router_model_id


def compute_blocked_model_ids(all_backend_ids: Sequence[str], checked_base_ids: Sequence[str]) -> List[str]:
    """blockedModelIds = every backend-discoverable base id that is not currently checked."""
    checked = set(checked_base_ids)
    return sorted(model_id for model_id in all_backend_ids if model_id not in checked)


def checked_base_ids_from_blocklist(all_items: Sequence[ModelItem], blocked_model_ids: Sequence[str]) -> List[str]:
    """Inverse of compute_blocked_model_ids: which base ids are checked, given a saved blocklist."""
    bases = {item.base_model_id for item in all_items}
    return sorted(base for base in bases if not is_blocked(base, blocked_model_ids))
