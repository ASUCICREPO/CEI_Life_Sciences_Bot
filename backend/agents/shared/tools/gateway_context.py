"""Bind a verified per-run Gateway token to the current asynchronous request.

Runtime callers can present custom headers, so the Gateway token must be independently
verified and bound to the already authenticated user and session before use.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from typing import Any, Iterator, Mapping

from shared.config import config
from shared.runtime.identity import CognitoAccessTokenVerifier, UnauthenticatedError

_token: ContextVar[str | None] = ContextVar("cei_gateway_token", default=None)


@lru_cache(maxsize=1)
def gateway_verifier() -> CognitoAccessTokenVerifier:
    client_id = os.environ.get("CEI_GATEWAY_CLIENT_ID", "").strip()
    if not client_id:
        raise UnauthenticatedError("gateway verification is not configured")
    return CognitoAccessTokenVerifier(config.cognito_user_pool_id, config.cognito_region or config.aws_region, [client_id])


def _header(context: Any) -> str | None:
    request = getattr(context, "request", None)
    headers = getattr(request, "headers", None) or getattr(context, "request_headers", None)
    if not isinstance(headers, Mapping):
        return None
    for name, value in headers.items():
        if isinstance(name, str) and name.lower() == "x-cei-gateway-token" and isinstance(value, str):
            return value
    return None


@contextmanager
def gateway_request_context(context: Any, user_id: str, session_id: str) -> Iterator[None]:
    if os.environ.get("CEI_REQUIRE_TOOL_GATEWAY", "").lower() != "true":
        yield
        return
    token = _header(context)
    if not token or len(token) > 4096:
        raise UnauthenticatedError("gateway credential is missing")
    claims = gateway_verifier().verify(token)
    if claims.get("cei_user_sub") != user_id or claims.get("cei_session_id") != session_id:
        raise UnauthenticatedError("gateway credential does not match this session")
    reset = _token.set(token)
    try:
        yield
    finally:
        _token.reset(reset)


def current_gateway_token() -> str | None:
    return _token.get()
