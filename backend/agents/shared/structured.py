"""Best-effort structured model calls that never raise.

Contract: ``model`` is a plain (unbound) chat model. This helper binds
``with_structured_output(schema, include_raw=True)`` itself on every call, so
callers pass a fresh ``get_model(...)`` result and never touch the binding.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import AIMessage

logger = logging.getLogger(__name__)


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
