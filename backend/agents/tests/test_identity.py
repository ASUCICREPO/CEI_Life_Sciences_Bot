"""CognitoAccessTokenVerifier: real RS256 verification against a locally generated JWKS."""
from __future__ import annotations

import base64
import json

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from shared.runtime import identity
from shared.runtime.identity import CognitoAccessTokenVerifier, UnauthenticatedError

POOL, REGION, CLIENT = "us-east-1_TestPool", "us-east-1", "client-abc"
ISS = f"https://cognito-idp.{REGION}.amazonaws.com/{POOL}"
NOW = 1_800_000_000


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _int_b64(n: int) -> str:
    return _b64(n.to_bytes((n.bit_length() + 7) // 8, "big"))


@pytest.fixture(scope="module")
def keypair():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwks(key, kid="k1") -> dict:
    pub = key.public_key().public_numbers()
    return {"keys": [{"kty": "RSA", "kid": kid, "alg": "RS256", "n": _int_b64(pub.n), "e": _int_b64(pub.e)}]}


def _token(key, claims: dict | None = None, *, kid="k1", alg="RS256") -> str:
    body = {"sub": "user-1", "iss": ISS, "token_use": "access", "client_id": CLIENT, "exp": NOW + 600,
            "iat": NOW - 10}
    body.update(claims or {})
    signing_input = f"{_b64(json.dumps({'alg': alg, 'kid': kid}).encode())}.{_b64(json.dumps(body).encode())}"
    sig = key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{_b64(sig)}"


def _verifier(key, fetches: list | None = None, kid="k1") -> CognitoAccessTokenVerifier:
    def fetch(url: str) -> dict:
        assert url == f"{ISS}/.well-known/jwks.json"
        if fetches is not None:
            fetches.append(url)
        return _jwks(key, kid)

    return CognitoAccessTokenVerifier(POOL, REGION, [CLIENT], fetch_jwks=fetch, clock=lambda: NOW)


def test_valid_access_token_returns_sub(keypair):
    assert _verifier(keypair).verify(_token(keypair))["sub"] == "user-1"


@pytest.mark.parametrize("claims", [
    {"iss": "https://cognito-idp.us-east-1.amazonaws.com/other"},
    {"token_use": "id"},
    {"client_id": "someone-else"},
    {"exp": NOW - 120},
    {"nbf": NOW + 600},
    {"sub": ""},
])
def test_claim_checks_reject(keypair, claims):
    with pytest.raises(UnauthenticatedError):
        _verifier(keypair).verify(_token(keypair, claims))


def test_tampered_payload_is_rejected(keypair):
    header, _, sig = _token(keypair).split(".")
    forged = _b64(json.dumps({"sub": "victim", "iss": ISS, "token_use": "access", "client_id": CLIENT,
                              "exp": NOW + 600}).encode())
    with pytest.raises(UnauthenticatedError):
        _verifier(keypair).verify(f"{header}.{forged}.{sig}")


def test_token_signed_by_another_key_is_rejected(keypair):
    attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(UnauthenticatedError):
        _verifier(keypair).verify(_token(attacker))


@pytest.mark.parametrize("token", ["", "a.b", "not.a.jwt", "x.y.z.w"])
def test_malformed_tokens_are_rejected(keypair, token):
    with pytest.raises(UnauthenticatedError):
        _verifier(keypair).verify(token)


def test_alg_none_and_hs256_are_rejected(keypair):
    for alg in ("none", "HS256"):
        with pytest.raises(UnauthenticatedError):
            _verifier(keypair).verify(_token(keypair, alg=alg))


def test_jwks_is_cached_and_unknown_kid_refetch_is_rate_limited(keypair):
    fetches: list = []
    v = _verifier(keypair, fetches)
    v.verify(_token(keypair))
    v.verify(_token(keypair))
    assert len(fetches) == 1
    for _ in range(3):
        with pytest.raises(UnauthenticatedError):
            v.verify(_token(keypair, kid="rotated"))
    assert len(fetches) == 1  # within JWKS_MIN_REFRESH_SECONDS: no refetch storm


def test_missing_cognito_config_fails_closed(monkeypatch):
    monkeypatch.setattr(identity.config, "cognito_user_pool_id", "")
    identity.set_verifier(None)
    with pytest.raises(UnauthenticatedError):
        identity.get_verifier().verify("anything")
