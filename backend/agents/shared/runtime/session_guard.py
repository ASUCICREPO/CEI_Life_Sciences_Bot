"""Session guard for the AgentCore entrypoints: authenticate, then prove session ownership, before anything else.

Every runtime wraps its entrypoint with `guarded_entrypoint`, so no op (run / resume / job_status) can reach
session state, replay fixtures, the in-memory LangGraph thread, or the job registry until:

1. the AgentCore runtime session id is a dedicated UUID (no default / fallback id exists),
2. the caller is the verified Cognito `sub` from the bearer token (identity.py), and
3. that user owns the session (fail closed: an owner lookup that throws refuses the request).

Error mapping (the body never carries internals; the real cause is logged to the runtime's CloudWatch log):
  invalid session id -> 400 | unauthenticated -> 401 | foreign session -> 404 | owner lookup failed -> 500
"""
from __future__ import annotations

import functools
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from starlette.responses import JSONResponse

from shared.persistence.chat_metadata import SessionNotFoundError
from shared.persistence.ownership import (
    GENERIC_INTERNAL_ERROR,
    OwnershipCheckError,
    is_session_uuid,
)
from shared.runtime import history
from shared.runtime.identity import UnauthenticatedError, resolve_user_id

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuthorizedSession:
    """A session id the caller has been proven to own (or that is new and will be stamped with the caller)."""

    user_id: str
    session_id: str


class InvalidSessionIdError(ValueError):
    """The request carries no runtime session id, or one that is not a UUID."""


def require_session_id(context: Any) -> str:
    session_id = getattr(context, "session_id", None)
    if not is_session_uuid(session_id):
        raise InvalidSessionIdError("a UUID runtime session id is required")
    return session_id


async def authorize_session(payload: Mapping[str, Any], context: Any) -> AuthorizedSession:
    """Session id -> verified user -> ownership. Raises; never returns an unchecked session."""
    session_id = require_session_id(context)
    user_id = resolve_user_id(payload, context)  # verified Cognito access-token sub; payload user_id ignored
    await history.get_recorder().assert_owner(user_id=user_id, session_id=session_id)
    return AuthorizedSession(user_id=user_id, session_id=session_id)


def error_response(status: int, message: str) -> JSONResponse:
    return JSONResponse({"status": "error", "message": message}, status_code=status)


Handler = Callable[[Mapping[str, Any], Any, AuthorizedSession], Awaitable[Any]]


def guarded_entrypoint(fn: Handler) -> Callable[[Mapping[str, Any], Any], Awaitable[Any]]:
    """Decorate `async def invoke(payload, context, session)`; apply *under* `@app.entrypoint`.

    The ownership errors are also caught when raised later in the handler (begin_turn / record_resume re-check).
    """

    @functools.wraps(fn)
    async def wrapper(payload: Mapping[str, Any], context: Any) -> Any:
        session_id = getattr(context, "session_id", None)
        try:
            session = await authorize_session(payload, context)
            return await fn(payload, context, session)
        except InvalidSessionIdError:
            log.warning("session guard: rejected request without a UUID session id")
            return error_response(400, "A valid session id is required.")
        except UnauthenticatedError as exc:
            log.warning("session guard: unauthenticated request (%s)", exc)
            return error_response(401, "Authentication is required.")
        except SessionNotFoundError:
            log.warning("session guard: refused session not owned by the caller session=%s", session_id)
            return error_response(404, "Session not found.")
        except OwnershipCheckError:
            # The real reason (with the chained store exception) goes to CloudWatch; the caller sees nothing of it.
            log.exception("session guard: ownership check failed, request refused session=%s", session_id)
            return error_response(500, GENERIC_INTERNAL_ERROR)

    return wrapper
