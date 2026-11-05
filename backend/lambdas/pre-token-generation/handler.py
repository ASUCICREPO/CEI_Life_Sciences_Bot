"""Cognito pre-token-generation trigger, event version V3_0 (F4, auth-and-tool-scope.md section 4).

For Router machine-to-machine tokens (client-credentials grant; the Router M2M client is the only app client
in the pool that has that grant) it copies
the user's Cognito `sub` and the session id from `aws_client_metadata` into two custom access-token
claims, `cei_user_sub` and `cei_session_id`. Cognito does not let a trigger add or modify `sub` itself,
so the claims are custom.

Every other token (a person signing in to the SPA, any other client) is returned unchanged. This trigger
runs on every token the user pool issues, so a bug here must never break SPA sign-in: user-token events
are passed through before any parsing happens.

Stdlib only. Logs outcomes only, never the metadata values (the user sub is PII-adjacent).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger()
logger.setLevel(logging.INFO)

CLIENT_CREDENTIALS_SOURCE = "TokenGeneration_ClientCredentials"
USER_SUB_CLAIM = "cei_user_sub"
SESSION_ID_CLAIM = "cei_session_id"
# Metadata keys the Router (and the token-refresh Lambda) send as aws_client_metadata
USER_SUB_KEY = "cei_user_sub"
SESSION_ID_KEY = "cei_session_id"

_SUB_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
# Same shape the session handlers accept (backend/lambdas/session-metadata-handler)
_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class InvalidClientMetadata(Exception):
    """Raised to make Cognito refuse a Router token whose metadata is present but malformed."""


def _claims_from_metadata(metadata: dict[str, Any]) -> dict[str, str]:
    claims: dict[str, str] = {}
    for key, claim, pattern in (
        (USER_SUB_KEY, USER_SUB_CLAIM, _SUB_PATTERN),
        (SESSION_ID_KEY, SESSION_ID_CLAIM, _SESSION_ID_PATTERN),
    ):
        if key not in metadata:
            continue
        value = metadata[key]
        if not isinstance(value, str) or not pattern.match(value):
            raise InvalidClientMetadata(f"{key} is not valid")
        claims[claim] = value
    return claims


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    # ADR: pass user-token events through before touching anything else | Rationale: this trigger fires on
    # SPA sign-in too; only client-credentials tokens are ours to change, and nothing below may run for a person
    if event.get("triggerSource") != CLIENT_CREDENTIALS_SOURCE:
        return event

    # ADR: no client-id check | Rationale: the Router M2M client is the only app client in this pool with the
    # client-credentials grant (asserted by a CDK test), so every client-credentials token here is a Router
    # token | Alternative: a ROUTER_CLIENT_IDS env var rejected — the pool references this function and the
    # client references the pool, so the client id in this function's environment is a CloudFormation cycle

    metadata = (event.get("request") or {}).get("clientMetadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    try:
        claims = _claims_from_metadata(metadata)
    except InvalidClientMetadata:
        # ADR: refuse the token | Rationale: a Router token with a forged or garbled user/session id must not
        # be issued at all; missing metadata (below) is allowed because the source Lambdas do not use it yet (B94)
        logger.warning(json.dumps({"event": "pre_token_m2m", "outcome": "invalid_metadata"}))
        raise

    if claims:
        response = event.setdefault("response", {})
        details = response.get("claimsAndScopeOverrideDetails") or {}
        access = details.get("accessTokenGeneration") or {}
        access["claimsToAddOrOverride"] = {**(access.get("claimsToAddOrOverride") or {}), **claims}
        details["accessTokenGeneration"] = access
        response["claimsAndScopeOverrideDetails"] = details
    logger.info(json.dumps({"event": "pre_token_m2m", "outcome": "ok", "claims": sorted(claims)}))
    return event
