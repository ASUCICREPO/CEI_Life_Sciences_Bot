"""Trusted caller identity for the agent runtimes.

The runtimes must never trust a `user_id` the client put in the payload: it would let any caller read or write
another user's conversations. The only identity is the `sub` of a Cognito **access** token presented as
`Authorization: Bearer <jwt>`.

ADR: verify the JWT in-process against the user pool's JWKS, even behind the AgentCore inbound JWT authorizer |
Rationale: defense in depth (plan 3b4d4f64 decision 9) — a mis-configured or bypassed authorizer must not turn into
cross-user access; the JWKS is cached per process, so the cost is one HTTPS fetch per cold start |
Alternative: decode-only (previous design) rejected; payload `user_id` / local `cli-user` fallback deleted — there is
no unauthenticated path, including local dev, and no runtime flag can disable verification. Tests inject a fake
verifier through `set_verifier` (dependency injection, not configuration).
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import threading
import time
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from shared.config import config

log = logging.getLogger(__name__)

CLOCK_SKEW_SECONDS = 60
JWKS_MIN_REFRESH_SECONDS = 60  # unknown-`kid` refetches are rate limited so random kids cannot hammer Cognito
JWKS_FETCH_TIMEOUT_SECONDS = 5


class UnauthenticatedError(PermissionError):
    """No verifiable identity on a request that requires one."""


class TokenVerifier(Protocol):
    def verify(self, token: str) -> Mapping[str, Any]:
        """Verified claims of `token`; raises UnauthenticatedError if it is not a valid access token."""


def _b64url_decode(segment: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    except (binascii.Error, ValueError) as exc:
        raise UnauthenticatedError("malformed token") from exc


def _b64url_int(segment: str) -> int:
    return int.from_bytes(_b64url_decode(segment), "big")


def _json_segment(segment: str) -> dict[str, Any]:
    try:
        value = json.loads(_b64url_decode(segment))
    except ValueError as exc:
        raise UnauthenticatedError("malformed token") from exc
    if not isinstance(value, dict):
        raise UnauthenticatedError("malformed token")
    return value


def _fetch_jwks(url: str) -> Mapping[str, Any]:
    with urllib.request.urlopen(url, timeout=JWKS_FETCH_TIMEOUT_SECONDS) as resp:  # noqa: S310 — fixed https URL
        return json.loads(resp.read())


class CognitoAccessTokenVerifier:
    """RS256 signature + issuer + `token_use=access` + `client_id` + expiry checks for one Cognito user pool."""

    def __init__(
        self,
        user_pool_id: str,
        region: str,
        app_client_ids: list[str],
        *,
        fetch_jwks: Callable[[str], Mapping[str, Any]] = _fetch_jwks,
        clock: Callable[[], float] = time.time,
        jwks_ttl_seconds: int = 3600,
        require_subject: bool = True,
    ) -> None:
        if not user_pool_id or not region or not app_client_ids:
            raise ValueError("Cognito user pool id, region and at least one app client id are required")
        self.issuer = f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}"
        self._jwks_url = f"{self.issuer}/.well-known/jwks.json"
        self._client_ids = frozenset(app_client_ids)
        self._fetch = fetch_jwks
        self._clock = clock
        self._ttl = jwks_ttl_seconds
        self._require_subject = require_subject
        self._keys: dict[str, rsa.RSAPublicKey] = {}
        self._fetched_at = float("-inf")
        self._lock = threading.Lock()

    def _refresh(self, *, force: bool) -> None:
        now = self._clock()
        age = now - self._fetched_at
        if (not force and age < self._ttl) or age < JWKS_MIN_REFRESH_SECONDS:
            return
        try:
            jwks = self._fetch(self._jwks_url)
        except Exception as exc:  # network/JSON failure: keep the cached keys, fail closed on unknown kids
            log.warning("JWKS fetch failed: %s", type(exc).__name__)
            return
        keys: dict[str, rsa.RSAPublicKey] = {}
        for jwk in jwks.get("keys", []):
            if jwk.get("kty") == "RSA" and jwk.get("kid") and jwk.get("n") and jwk.get("e"):
                keys[jwk["kid"]] = rsa.RSAPublicNumbers(_b64url_int(jwk["e"]), _b64url_int(jwk["n"])).public_key()
        self._keys = keys
        self._fetched_at = now

    def _key(self, kid: str) -> rsa.RSAPublicKey:
        with self._lock:
            self._refresh(force=False)
            if kid not in self._keys:
                self._refresh(force=True)  # key rotation
            key = self._keys.get(kid)
        if key is None:
            raise UnauthenticatedError("unknown signing key")
        return key

    def verify(self, token: str) -> Mapping[str, Any]:
        parts = token.split(".")
        if len(parts) != 3:
            raise UnauthenticatedError("malformed token")
        header, claims = _json_segment(parts[0]), _json_segment(parts[1])
        if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
            raise UnauthenticatedError("unsupported token algorithm")
        try:
            self._key(header["kid"]).verify(
                _b64url_decode(parts[2]), f"{parts[0]}.{parts[1]}".encode("ascii"),
                padding.PKCS1v15(), hashes.SHA256(),
            )
        except (InvalidSignature, UnicodeEncodeError) as exc:
            raise UnauthenticatedError("invalid token signature") from exc

        now = self._clock()
        if claims.get("iss") != self.issuer:
            raise UnauthenticatedError("wrong token issuer")
        if claims.get("token_use") != "access":
            raise UnauthenticatedError("not an access token")
        if claims.get("client_id") not in self._client_ids:
            raise UnauthenticatedError("token issued to an unknown client")
        exp = claims.get("exp")
        if not isinstance(exp, (int, float)) or exp + CLOCK_SKEW_SECONDS <= now:
            raise UnauthenticatedError("token expired")
        for name in ("nbf", "iat"):
            value = claims.get(name)
            if isinstance(value, (int, float)) and value - CLOCK_SKEW_SECONDS > now:
                raise UnauthenticatedError("token not yet valid")
        sub = claims.get("sub")
        if self._require_subject and (not isinstance(sub, str) or not sub):
            raise UnauthenticatedError("token has no subject")
        return claims


def verify_gateway_token(token: str, verifier: TokenVerifier, scopes: tuple[str, ...], user_id: str, session_id: str) -> list[str]:
    """Validate the signed M2M grant and its run binding without exposing token contents."""
    claims = verifier.verify(token)
    scope = claims.get("scope")
    if not isinstance(scope, str) or sorted(scope.split()) != sorted(scopes):
        raise ValueError("incorrect token grant")
    if claims.get("cei_user_sub") != user_id or claims.get("cei_session_id") != session_id:
        raise ValueError("incorrect token binding")
    return sorted(scope.split())


class _UnconfiguredVerifier:
    """Fail closed when the runtime has no Cognito settings: every request is rejected."""

    def verify(self, token: str) -> Mapping[str, Any]:
        raise UnauthenticatedError("token verification is not configured (CEI_COGNITO_* settings missing)")


_verifier: TokenVerifier | None = None
_verifier_lock = threading.Lock()


def _build_verifier() -> TokenVerifier:
    try:
        return CognitoAccessTokenVerifier(
            config.cognito_user_pool_id,
            config.cognito_region or config.aws_region,
            list(config.cognito_app_client_ids),
            jwks_ttl_seconds=config.cognito_jwks_ttl_seconds,
        )
    except ValueError:
        log.error("CEI_COGNITO_USER_POOL_ID / CEI_COGNITO_APP_CLIENT_IDS unset; rejecting all requests")
        return _UnconfiguredVerifier()


def get_verifier() -> TokenVerifier:
    global _verifier
    with _verifier_lock:
        if _verifier is None:
            _verifier = _build_verifier()
        return _verifier


def set_verifier(verifier: TokenVerifier | None) -> None:
    """Dependency-injection seam for tests; `None` rebuilds the Cognito verifier from config on next use."""
    global _verifier
    with _verifier_lock:
        _verifier = verifier


def _header(headers: Mapping[str, str] | None, name: str) -> str | None:
    if not headers:
        return None
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def bearer_token(context: Any) -> str | None:
    auth = _header(getattr(context, "request_headers", None), "Authorization")
    if not auth or not auth.lower().startswith("bearer "):
        return None
    token = auth[7:].strip()
    return token or None


def resolve_user_id(payload: Mapping[str, Any], context: Any) -> str:
    """The caller's Cognito `sub`, from a verified access token. Raises UnauthenticatedError otherwise.

    A `user_id` in the payload is never used; if present and different it is logged and ignored.
    """
    token = bearer_token(context)
    if token is None:
        raise UnauthenticatedError("no bearer token on this request")
    sub = str(get_verifier().verify(token)["sub"])
    claimed = payload.get("user_id")
    if claimed not in (None, sub):
        log.warning("ignoring payload user_id that does not match the authenticated caller")
    return sub
