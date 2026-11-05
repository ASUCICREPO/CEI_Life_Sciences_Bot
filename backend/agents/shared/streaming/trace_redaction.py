"""B7 never-show redaction for reasoning / tool-call traces.

Traces reach users two ways: the formatted `trace_text` appended to a runtime response, and the per-turn trace
persisted by TraceRepository and served by GET /sessions/{sessionId}/turns/{messageId}/trace. Neither may reveal
Bedrock model ids/ARNs, guardrail ids, system prompts, prompt versions, or secrets.

Event shape: `trace_event` from .agents/specs/streaming-protocol.md (ts, step_type, tool_name, tool_input,
tool_output_summary, sources, reasoning_text, reasoning_label) plus the envelope `seq`/`type`.

Two mechanisms:
  * an ALLOW-LIST of `trace_event` fields — anything else is dropped; inside `tool_input`, keys that are on the
    never-show list or match a deny pattern are dropped;
  * DENY patterns over every string value — a matching value is replaced wholesale with REDACTED (fail closed:
    a partial scrub could still leak the shape of a secret).

Decorators wrap the trace producers so their output is redacted at the source; TraceRepository.save redacts again.
"""
from __future__ import annotations

import functools
import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any, TypeVar

REDACTED = "[redacted]"

TRACE_EVENT_FIELDS: frozenset[str] = frozenset(
    {
        "seq",
        "type",
        "ts",
        "step_type",
        "tool_name",
        "tool_input",
        "tool_output_summary",
        "sources",
        "reasoning_text",
        "reasoning_label",
    }
)
# streaming-protocol.md "Never present, per B7"
NEVER_SHOW_KEYS: frozenset[str] = frozenset(
    {"model_id", "modelid", "guardrail_config", "guardrail_id", "system_prompt", "prompt_version", "api_key", "secret"}
)
_MAX_NESTING = 8

DENY_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        # Bedrock model ids (incl. cross-region inference profiles) and ARNs
        r"\b(?:us|eu|apac|global)\.(?:anthropic|amazon|meta|cohere|mistral|ai21|deepseek)\.",
        r"\b(?:anthropic|amazon|meta|cohere|mistral|ai21|deepseek)\.[a-z0-9-]+-v?\d",
        r"\banthropic\.claude",
        r"arn:aws:bedrock:",
        # guardrails
        r"guardrail",
        # system prompt / prompt version
        r"system[\s_-]*prompt",
        r"prompt[\s_-]*version",
        # secrets
        r"arn:aws:secretsmanager:",
        r"\bAKIA[0-9A-Z]{16}\b",
        r"\bsk-[A-Za-z0-9_-]{8,}",
        r"api[\s_-]*key",
        r"\bBearer\s+",
    )
)


def is_sensitive(value: str) -> bool:
    return any(pattern.search(value) for pattern in DENY_PATTERNS)


def redact_text(value: str) -> str:
    """REDACTED if `value` matches any deny pattern, else `value` unchanged."""
    return REDACTED if is_sensitive(value) else value


def _redact_value(value: Any, depth: int = 0) -> Any:
    """Deny-scan a JSON-like value; None means 'drop it'."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    if depth >= _MAX_NESTING:
        return None
    if isinstance(value, Mapping):
        safe: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if name.lower() in NEVER_SHOW_KEYS or is_sensitive(name):
                continue
            redacted = _redact_value(item, depth + 1)
            if redacted is not None:
                safe[name] = redacted
        return safe
    if isinstance(value, (list, tuple)):
        return [item for item in (_redact_value(v, depth + 1) for v in value) if item is not None]
    return None


def redact_trace_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Allow-listed, deny-scanned copy of one trace event."""
    safe: dict[str, Any] = {}
    for key, value in event.items():
        if key not in TRACE_EVENT_FIELDS:
            continue
        if key == "tool_input" and not isinstance(value, Mapping):
            continue
        if key == "sources" and not isinstance(value, (list, tuple)):
            continue
        redacted = _redact_value(value)
        if redacted is not None:
            safe[key] = redacted
    return safe


def _field(entry: Any, name: str, default: Any) -> Any:
    if isinstance(entry, Mapping):
        return entry.get(name, default)
    return getattr(entry, name, default)


def redact_entries(entries: Iterable[Any] | None) -> list[Any]:
    """TraceEntry copies (stage/detail deny-scanned) of agent trace entries, accepting models or dicts."""
    from shared.state import TraceEntry

    return [
        TraceEntry(
            stage=redact_text(str(_field(entry, "stage", ""))),
            detail=redact_text(str(_field(entry, "detail", ""))),
            input_tokens=int(_field(entry, "input_tokens", 0) or 0),
            output_tokens=int(_field(entry, "output_tokens", 0) or 0),
        )
        for entry in entries or []
    ]


def trace_events_from_entries(entries: Iterable[Any] | None) -> list[dict[str, Any]]:
    """Agent TraceEntry list -> redacted `trace_event` dicts (the stream / stored-trace contract).

    Agent TraceEntries are stage/detail summaries written by our nodes, not raw model reasoning, so they map to
    `step_type: "reasoning"` with `reasoning_label: "summarized"`.
    """
    events = []
    for seq, entry in enumerate(entries or []):
        stage = str(_field(entry, "stage", ""))
        detail = str(_field(entry, "detail", ""))
        events.append(
            redact_trace_event(
                {
                    "seq": seq,
                    "type": "trace_event",
                    "step_type": "reasoning",
                    # scan stage and detail separately so a sensitive detail cannot hide a safe stage, and vice versa
                    "reasoning_text": f"[{redact_text(stage)}] {redact_text(detail)}",
                    "reasoning_label": "summarized",
                }
            )
        )
    return events


F = TypeVar("F", bound=Callable[..., Any])


def redacts_trace_input(fn: F) -> F:
    """Decorator for formatters `fn(entries, *args)`: entries are redacted before formatting."""

    @functools.wraps(fn)
    def wrapper(entries: Any, *args: Any, **kwargs: Any) -> Any:
        return fn(redact_entries(entries), *args, **kwargs)

    return wrapper  # type: ignore[return-value]


def redacts_trace_output(fn: F) -> F:
    """Decorator for producers returning a TraceEntry list: the returned entries are redacted."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return redact_entries(fn(*args, **kwargs))

    return wrapper  # type: ignore[return-value]
