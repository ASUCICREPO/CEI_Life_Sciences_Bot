"""Cognito pre-sign-up trigger (B16): reject sign-up unless the user's email
domain exactly matches one entry in the ALLOWED_EMAIL_DOMAINS allowlist.

Stdlib only. Never logs the email address (PII) — only the pass/fail outcome.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_ENV_VAR = "ALLOWED_EMAIL_DOMAINS"

# ADR: ascii-only, no whitespace domain shape | Rationale: security review — a domain must look like a
# real DNS name before it is ever compared against the allowlist; this also rejects embedded whitespace
_DOMAIN_SHAPE_PATTERN = re.compile(r"^[a-z0-9.-]+$")


def _allowed_domains() -> set[str]:
    raw = os.environ.get(_ENV_VAR, "")
    return {domain.strip().lower() for domain in raw.split(",") if domain.strip()}


def _domain_of(email: Any) -> str | None:
    """Lowercase, exact-match domain after '@'. None for missing/malformed/non-ascii input."""
    if not isinstance(email, str) or not email:
        return None
    # ADR: exactly one '@' | Rationale: security review — rpartition silently tolerated a second '@' in the
    # local part (e.g. "a@b@evil.com"); require the address to have exactly one '@' before splitting it
    if email.count("@") != 1:
        return None
    local, _sep, domain = email.partition("@")
    if not local or not domain:
        return None
    domain = domain.strip().lower()
    if not domain.isascii():
        return None
    if not _DOMAIN_SHAPE_PATTERN.match(domain):
        return None
    return domain


def handler(event: dict, _context: Any) -> dict:
    email = event.get("request", {}).get("userAttributes", {}).get("email")
    domain = _domain_of(email)
    allowed = _allowed_domains()

    if domain is None or domain not in allowed:
        # ADR: log only the outcome, never the email | Rationale: PII must not enter CloudWatch Logs
        logger.info(json.dumps({"event": "pre_signup_domain_check", "allowed": False}))
        raise Exception("Sign-up is not allowed for this email domain.")

    logger.info(json.dumps({"event": "pre_signup_domain_check", "allowed": True}))
    return event
