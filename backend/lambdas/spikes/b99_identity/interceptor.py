"""Isolated B99 experiment. Do not attach this interceptor to a production Gateway.

The Gateway must pass request headers and the target must be reachable only through
the tested Gateway. Injecting a tool argument is safe only if those assumptions are
established by the deployed spike; no application code consumes this value yet.
"""
from __future__ import annotations

import copy
import os
import re
from functools import lru_cache
from typing import Any

from shared.runtime.identity import CognitoAccessTokenVerifier

_ID = re.compile(r"^[A-Za-z0-9-]{1,128}$")
_CONTEXT = "_cei_spike_identity"


@lru_cache(maxsize=1)
def verifier() -> CognitoAccessTokenVerifier:
    return CognitoAccessTokenVerifier(
        os.environ["USER_POOL_ID"], os.environ["AWS_REGION"],
        [os.environ["ROUTER_CLIENT_ID"]], require_subject=False,
    )


def _denied(request_id: Any) -> dict[str, Any]:
    return {"interceptorOutputVersion": "1.0", "mcp": {"transformedGatewayResponse": {
        "statusCode": 403,
        "body": {"jsonrpc": "2.0", "id": request_id,
                 "error": {"code": -32001, "message": "Unauthorized"}},
    }}}


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    request = event.get("mcp", {}).get("gatewayRequest", {})
    body = request.get("body")
    if not isinstance(body, dict):
        return _denied(None)
    headers = request.get("headers") or {}
    if not isinstance(headers, dict):
        return _denied(body.get("id"))
    auth = next((v for k, v in headers.items() if isinstance(k, str) and k.lower() == "authorization"), None)
    try:
        if not isinstance(auth, str) or not auth.lower().startswith("bearer "):
            raise ValueError("missing bearer")
        claims = verifier().verify(auth[7:].strip())
        user, session, scope = claims.get("cei_user_sub"), claims.get("cei_session_id"), claims.get("scope")
        required = os.environ["SPIKE_SCOPE"]
        if not isinstance(user, str) or not _ID.fullmatch(user):
            raise ValueError("invalid user")
        if not isinstance(session, str) or not _ID.fullmatch(session):
            raise ValueError("invalid session")
        if not isinstance(scope, str) or required not in scope.split():
            raise ValueError("missing scope")
        transformed = copy.deepcopy(body)
        if transformed.get("method") == "tools/call":
            params = transformed.get("params")
            if not isinstance(params, dict) or not isinstance(params.get("arguments"), dict):
                raise ValueError("invalid arguments")
            # Always replace any value supplied by the client or model.
            params["arguments"][_CONTEXT] = {"user_id": user, "session_id": session}
        return {"interceptorOutputVersion": "1.0", "mcp": {"transformedGatewayRequest": {"body": transformed}}}
    except Exception:
        # Fail closed without logging bearer tokens, arguments, or claims.
        return _denied(body.get("id"))
