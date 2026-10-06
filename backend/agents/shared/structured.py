"""Best-effort structured model calls that never raise.

Contract: ``model`` is a plain (unbound) chat model. This helper binds
``with_structured_output(schema, include_raw=True)`` itself on every call, so
callers pass a fresh ``get_model(...)`` result and never touch the binding.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.messages import AIMessage

logger = logging.getLogger(__name__)


def _repair_stringified_fields(schema: type, raw: AIMessage | None) -> Any | None:
    """Re-validate tool-call args after decoding top-level fields that hold JSON text.

    Models sometimes send a list or object field (for example ``sections``) as a
    JSON string. Only top-level ``str`` values that start with ``[`` or ``{`` are
    decoded; anything else is left as the model sent it.
    """
    tool_calls = getattr(raw, "tool_calls", None) or []
    if not tool_calls or not hasattr(schema, "model_validate"):
        return None
    args = tool_calls[0].get("args")
    if not isinstance(args, dict):
        return None
    repaired: dict[str, Any] = {}
    changed = False
    for key, value in args.items():
        if isinstance(value, str) and value.lstrip()[:1] in ("[", "{"):
            try:
                repaired[key] = json.loads(value)
                changed = True
                continue
            except ValueError:
                pass
        repaired[key] = value
    if not changed:
        return None
    try:
        return schema.model_validate(repaired)
    except Exception:  # ADR: repair is best effort | the caller still has its prose fallback
        return None


def invoke_structured(
    model: Any,
    schema: type,
    messages: list[dict[str, str]],
) -> tuple[Any | None, AIMessage | None, str | None]:
    """Invoke ``model`` for a structured ``schema`` response.

    Returns ``(parsed, raw, error)``. ``parsed`` is ``None`` on any failure —
    a raised exception, a model-reported ``parsing_error``, or a missing
    ``parsed`` value — so callers can fall back to a plain-text path.
    """
    try:
        structured_model = model.with_structured_output(schema, include_raw=True)
        result = structured_model.invoke(messages)
    except Exception as exc:  # ADR: catch-all | callers need a guaranteed fallback, never a crash
        logger.warning({"event": "invoke_structured_error", "schema": schema.__name__, "error": repr(exc)})
        return None, None, repr(exc)

    if not isinstance(result, dict):
        return None, None, f"unexpected structured result type: {type(result).__name__}"

    raw = result.get("raw")
    parsing_error = result.get("parsing_error")
    if parsing_error is not None:
        repaired = _repair_stringified_fields(schema, raw)
        if repaired is not None:
            logger.info({"event": "invoke_structured_repaired", "schema": schema.__name__})
            return repaired, raw, None
        logger.warning({
            "event": "invoke_structured_parsing_error",
            "schema": schema.__name__,
            "error": repr(parsing_error),
        })
        return None, raw, repr(parsing_error)

    parsed = result.get("parsed")
    if parsed is None:
        return None, raw, "structured output missing 'parsed'"

    return parsed, raw, None
