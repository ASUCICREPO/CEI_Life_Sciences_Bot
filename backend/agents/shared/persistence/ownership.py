"""Session ownership: the one check every session-scoped request runs before it touches the session.

Shared by the AgentCore runtimes (shared/runtime/session_guard.py) and the REST Lambdas (shipped in the
python-deps layer with the rest of shared/persistence). The check fails closed: if the owner cannot be read,
the request is refused with OwnershipCheckError — it never proceeds as if the caller owned the session.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from shared.persistence.chat_metadata import SessionNotFoundError

if TYPE_CHECKING:
    from shared.persistence.chat_history import BaseChatHistoryRepository

# The only text a caller ever sees for a server-side failure; the real cause is logged, never returned.
GENERIC_INTERNAL_ERROR = "It's not you, it's us. Something went wrong"

# A dedicated, canonical UUID (8-4-4-4-12 hex). There is no default or fallback session id.
_SESSION_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class OwnershipCheckError(Exception):
    """The session owner could not be determined (store failure). Always mapped to a generic HTTP 500.

    The underlying exception is chained as ``__cause__`` so the caller's ``log.exception`` records it.
    """


def is_session_uuid(value: Any) -> bool:
    # fullmatch, not match: with `$`, re.match accepts a trailing "\n" ("<uuid>\n")
    return isinstance(value, str) and _SESSION_UUID.fullmatch(value) is not None


def verify_session_owner(history: BaseChatHistoryRepository, user_id: str, session_id: str) -> None:
    """Raise unless ``user_id`` may use ``session_id``.

    - SessionNotFoundError: the session id is stamped with another owner (callers answer "not found").
    - OwnershipCheckError: the owner lookup itself failed; the request must not run.

    A session with no history yet (``owner_of`` -> None) passes: its first turn stamps the caller as owner.
    Session-id *format* (UUID only) is enforced at the entry points (runtime session guard, REST handlers, proxy).
    """
    if not user_id:
        raise OwnershipCheckError("no authenticated user for the ownership check")
    try:
        owner = history.owner_of(session_id)
    except Exception as exc:  # noqa: BLE001 — fail closed; the cause travels as __cause__ to the caller's log
        raise OwnershipCheckError(f"owner lookup failed for session {session_id}: {type(exc).__name__}") from exc
    if owner is not None and owner != user_id:
        raise SessionNotFoundError(session_id)
