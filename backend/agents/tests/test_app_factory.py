"""CORS preflight for direct browser calls to the AgentCore runtime container (shared/runtime/app_factory.py)."""
from __future__ import annotations

import pytest
from starlette.middleware.cors import CORSMiddleware
from starlette.testclient import TestClient

from shared.config import AgentConfig, parse_origins
from shared.runtime.app_factory import SESSION_ID_HEADER, create_app

SITE = "https://d1234abcd.cloudfront.net"
LOCAL = "http://localhost:5173"


def _app(origins):
    app = create_app(origins)

    @app.entrypoint
    def invoke(payload, context):  # noqa: ANN001
        return {"text": "ok", "status": "completed"}

    return app


def _preflight(client: TestClient, origin: str):
    return client.options(
        "/invocations",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": f"authorization,content-type,accept,{SESSION_ID_HEADER}",
        },
    )


def test_preflight_from_allowed_origin_succeeds():
    client = TestClient(_app([SITE, LOCAL]))
    response = _preflight(client, SITE)
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == SITE
    assert "POST" in response.headers["access-control-allow-methods"]
    allowed = response.headers["access-control-allow-headers"].lower()
    for header in ("authorization", "content-type", "accept", SESSION_ID_HEADER):
        assert header in allowed
    assert response.headers["access-control-max-age"] == "3600"
    assert "access-control-allow-credentials" not in response.headers


def test_preflight_from_disallowed_origin_gets_no_allow_origin():
    client = TestClient(_app([SITE]))
    response = _preflight(client, "https://evil.example.com")
    assert "access-control-allow-origin" not in response.headers


def test_post_from_allowed_origin_carries_allow_origin():
    client = TestClient(_app([SITE]))
    response = client.post(
        "/invocations",
        json={"prompt": "hi"},
        headers={"Origin": SITE, SESSION_ID_HEADER: "s" * 36},
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == SITE


def test_no_origins_means_no_cors_middleware():
    app = _app([])
    assert not any(m.cls is CORSMiddleware for m in app.user_middleware)
    response = _preflight(TestClient(app), SITE)
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", []),
        (None, []),
        (f" {SITE}/ , ,{LOCAL}", [SITE, LOCAL]),
        ("*", []),  # wildcard is never honoured
    ],
)
def test_parse_origins(raw, expected):
    assert parse_origins(raw) == expected


def test_env_var_feeds_config(monkeypatch):
    monkeypatch.setenv("CEI_ALLOWED_ORIGINS", f"{SITE},{LOCAL}")
    assert AgentConfig().allowed_origins_list == [SITE, LOCAL]
    monkeypatch.delenv("CEI_ALLOWED_ORIGINS")
    assert AgentConfig().allowed_origins_list == []
