"""Research routing core, not yet exposed as a Lambda or connected to a stream.

The route must independently verify the caller's Cognito access token and session
ownership before asking Cognito for any Gateway token. Injected functions keep the
credentials and runtime transport server-side and make that ordering testable.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from uuid import uuid4

from shared.persistence.active_runs import ActiveRunRepository
from shared.persistence.ownership import is_session_uuid
from shared.runtime.identity import TokenVerifier, UnauthenticatedError


class InvalidResearchRequest(ValueError):
    pass


class SessionNotOwned(PermissionError):
    pass


class TokenGrantError(RuntimeError):
    pass


async def route_research(
    *,
    bearer_token: str,
    request: Mapping[str, Any],
    verifier: TokenVerifier,
    session_exists: Callable[[str, str], bool],
    leases: ActiveRunRepository,
    scopes: tuple[str, ...],
    issue_token: Callable[[str, str, tuple[str, ...]], Awaitable[Mapping[str, Any]]],
    invoke_runtime: Callable[[str, str, str, Mapping[str, Any]], Awaitable[Any]],
) -> Any:
    """Authenticate, prove ownership, acquire a slot, mint a scoped token, invoke.

    `issue_token` receives (verified sub, session id, server-configured scopes).
    `invoke_runtime` receives (user bearer, Gateway bearer, session id, payload).
    These callables must be implemented by the trusted Router, not user input.
    """
    if not isinstance(bearer_token, str) or not bearer_token:
        raise UnauthenticatedError("Authentication is required.")
    claims = verifier.verify(bearer_token)
    user_id = claims.get("sub")
    if not isinstance(user_id, str) or not user_id:
        raise UnauthenticatedError("Authentication is required.")
    session_id = request.get("session_id")
    message = request.get("message")
    mode = request.get("mode", "science")
    if (not is_session_uuid(session_id) or not isinstance(message, str) or not message.strip()
            or request.get("task_type") != "live-research" or mode not in ("science", "business")):
        raise InvalidResearchRequest("The Research request is invalid.")
    if not scopes or any(not isinstance(scope, str) or not scope.startswith("cei-tools/") for scope in scopes):
        raise TokenGrantError("The tool access configuration is invalid.")
    if not session_exists(user_id, session_id):
        raise SessionNotOwned("Session not found.")
    lease = leases.acquire(user_id, session_id, str(uuid4()), scopes)
    try:
        grant = await issue_token(user_id, session_id, scopes)
        token = grant.get("access_token")
        granted = grant.get("scope")
        if (not isinstance(token, str) or not token or not isinstance(granted, str)
                or sorted(granted.split()) != sorted(scopes)):
            raise TokenGrantError("The tool access grant is invalid.")
        return await invoke_runtime(bearer_token, token, session_id, {"prompt": message, "mode": mode})
    finally:
        leases.release(lease)
