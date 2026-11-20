"""Cognito pre-token-generation trigger, event version V3_0 (F4, auth-and-tool-scope.md section 4).

For Router machine-to-machine tokens (client-credentials grant; the Router M2M client is the only app client
in the pool that has that grant) it copies
the user's Cognito `sub` and the session id from `aws_client_metadata` into two custom access-token
claims, `cei_user_sub` and `cei_session_id`. Cognito does not let a trigger add or modify `sub` itself,
so the claims are custom.

Every user-token event (hosted-UI sign-in, refresh, etc. — anything that is not client-credentials) instead
goes through the B112 age-verification branch: when `request.userAttributes.birthdate` is present, a valid
calendar date, and the user is 18+ today (UTC), it adds the access-token claim `cei_age_verified: "true"`.
That claim is the run-start/write gate every REST route and agent runtime checks later
(`http_errors.require_age_verified`, `shared.runtime.identity`); `birthdate` itself is written once by
`lambdas/profile-handler/handler.py`.

This trigger runs on every token the user pool issues, so a bug here must never break SPA sign-in: the
user-token branch catches every exception, logs only the outcome, and returns the event unmodified.

Stdlib only. Logs outcomes only, never the metadata values or birthdate (PII-adjacent).
"""

from __future__ import annotations

import copy
import json
import logging
import re
from datetime import UTC, date, datetime
from typing import Any

logger = logging.getLogger()
logger.setLevel(logging.INFO)

CLIENT_CREDENTIALS_SOURCE = "TokenGeneration_ClientCredentials"
USER_SUB_CLAIM = "cei_user_sub"
SESSION_ID_CLAIM = "cei_session_id"
# Metadata keys the Router (and the token-refresh Lambda) send as aws_client_metadata
USER_SUB_KEY = "cei_user_sub"
SESSION_ID_KEY = "cei_session_id"

# B112: the run-start/write gate claim; see module docstring.
AGE_VERIFIED_CLAIM = "cei_age_verified"
MIN_AGE_YEARS = 18

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


def _is_adult(birth: date, *, today: date) -> bool:
    age = today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))
    return age >= MIN_AGE_YEARS


def _with_access_token_claim(event: dict[str, Any], claim: str, value: str) -> dict[str, Any]:
    """A deep copy of `event` with one access-token claim added/overridden. The original is untouched."""
    updated = copy.deepcopy(event)
    response = updated.setdefault("response", {})
    details = response.get("claimsAndScopeOverrideDetails") or {}
    access = details.get("accessTokenGeneration") or {}
    access["claimsToAddOrOverride"] = {**(access.get("claimsToAddOrOverride") or {}), claim: value}
    details["accessTokenGeneration"] = access
    response["claimsAndScopeOverrideDetails"] = details
    return updated


def _handle_client_credentials(event: dict[str, Any]) -> dict[str, Any]:
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


def _handle_user_token(event: dict[str, Any]) -> dict[str, Any]:
    # ADR: catch every exception here | Rationale: this branch runs on every SPA sign-in/refresh; a bug in
    # birthdate parsing must never block a user from signing in | Alternative: letting it propagate rejected
    # — Cognito would refuse the token entirely, unlike the M2M branch where refusal is the intended outcome
    try:
        attributes = (event.get("request") or {}).get("userAttributes") or {}
        birthdate_raw = attributes.get("birthdate") if isinstance(attributes, dict) else None
        if not isinstance(birthdate_raw, str) or not birthdate_raw:
            logger.info(json.dumps({"event": "pre_token_user", "outcome": "no_birthdate"}))
            return event
        birth = date.fromisoformat(birthdate_raw)
        if not _is_adult(birth, today=datetime.now(UTC).date()):
            logger.info(json.dumps({"event": "pre_token_user", "outcome": "underage"}))
            return event
        updated = _with_access_token_claim(event, AGE_VERIFIED_CLAIM, "true")
        logger.info(json.dumps({"event": "pre_token_user", "outcome": "ok"}))
        return updated
    except Exception:
        logger.exception("pre_token_user_failed")
        return event


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    if event.get("triggerSource") == CLIENT_CREDENTIALS_SOURCE:
        return _handle_client_credentials(event)
    return _handle_user_token(event)
