import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import handler  # noqa: E402


def _event(email=None):
    user_attributes = {}
    if email is not None:
        user_attributes["email"] = email
    return {"request": {"userAttributes": user_attributes}}


def test_allowed_domain_passes(monkeypatch):
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "example.com,partner.org")
    event = _event("user@example.com")
    assert handler.handler(event, None) == event


def test_disallowed_domain_rejects(monkeypatch):
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "example.com")
    with pytest.raises(Exception, match="Sign-up is not allowed"):
        handler.handler(_event("user@evil.com"), None)


def test_subdomain_is_not_an_exact_match(monkeypatch):
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "example.com")
    with pytest.raises(Exception, match="Sign-up is not allowed"):
        handler.handler(_event("user@mail.example.com"), None)


def test_uppercase_email_and_allowlist_entry_match_case_insensitively(monkeypatch):
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "Example.COM")
    event = _event("User@EXAMPLE.com")
    assert handler.handler(event, None) == event


def test_malformed_email_rejects(monkeypatch):
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "example.com")
    with pytest.raises(Exception, match="Sign-up is not allowed"):
        handler.handler(_event("not-an-email"), None)


def test_missing_email_attribute_rejects(monkeypatch):
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "example.com")
    with pytest.raises(Exception, match="Sign-up is not allowed"):
        handler.handler(_event(email=None), None)


def test_empty_allowlist_rejects_every_domain(monkeypatch):
    monkeypatch.delenv("ALLOWED_EMAIL_DOMAINS", raising=False)
    with pytest.raises(Exception, match="Sign-up is not allowed"):
        handler.handler(_event("user@example.com"), None)


@pytest.mark.parametrize("raw", ["", " ", " , ,"])
def test_blank_allowlist_value_rejects_every_domain(monkeypatch, raw):
    # CDK writes ALLOWED_EMAIL_DOMAINS="" when there is no deploy config: that must fail closed too
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", raw)
    with pytest.raises(Exception, match="Sign-up is not allowed"):
        handler.handler(_event("user@example.com"), None)


def test_two_at_signs_rejects(monkeypatch):
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "example.com")
    with pytest.raises(Exception, match="Sign-up is not allowed"):
        handler.handler(_event("user@evil.com@example.com"), None)


def test_kelvin_sign_domain_normalizes_to_ascii_k_via_lower(monkeypatch):
    # U+212A KELVIN SIGN lowercases to ASCII 'k' in Python — confirm this normalizes
    # consistently (matches only the literal ASCII domain it lowercases to) rather than
    # crashing or silently bypassing the isascii()/shape check.
    kelvin_domain = "Kelvin.example"
    assert kelvin_domain != "kelvin.example"  # not equal before normalization
    assert kelvin_domain.lower() == "kelvin.example"  # equal after Python's str.lower()
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "kelvin.example")
    event = _event(f"user@{kelvin_domain}")
    assert handler.handler(event, None) == event


def test_unicode_domain_rejects(monkeypatch):
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "xn--mnchen-3ya.example,münchen.example")
    with pytest.raises(Exception, match="Sign-up is not allowed"):
        handler.handler(_event("user@münchen.example"), None)


def test_domain_with_internal_whitespace_rejects(monkeypatch):
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "example.com")
    with pytest.raises(Exception, match="Sign-up is not allowed"):
        handler.handler(_event("user@exa mple.com"), None)
