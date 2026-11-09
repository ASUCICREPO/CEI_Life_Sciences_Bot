import base64
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location("client_diagnostics_handler", Path(__file__).parents[1] / "handler.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def event(payload):
    return {"headers": {"Origin": "https://site.example"}, "requestContext": {"authorizer": {"claims": {"sub": "user-secret"}}},
            "body": json.dumps(payload)}


def test_only_allowlisted_diagnostic_fields_are_logged(monkeypatch, caplog):
    monkeypatch.setenv("ALLOWED_ORIGINS", "https://site.example")
    payload = {"source": "stream", "failure": "http_error", "status": 502,
               "request_id": "87c2b902-a93f-46e3-a066-af38f8360d83"}
    with caplog.at_level("INFO"):
        response = module.handler(event(payload), SimpleNamespace(aws_request_id="lambda-id"))
    assert response["statusCode"] == 204
    assert response["headers"]["Access-Control-Allow-Origin"] == "https://site.example"
    assert '"event": "browser_failure"' in caplog.text
    assert "87c2b902-a93f-46e3-a066-af38f8360d83" in caplog.text
    assert "user-secret" not in caplog.text


@pytest.mark.parametrize("payload", [
    {"source": "stream", "failure": "network", "message": "private prompt"},
    {"source": ["stream"], "failure": "network"},
    {"source": "stream", "failure": "private prompt"},
    {"source": "stream", "failure": "network", "status": True},
    {"source": "stream", "failure": "network", "request_id": "private prompt"},
])
def test_rejects_unknown_or_sensitive_fields_without_logging(payload, caplog):
    assert module.handler(event(payload), None)["statusCode"] == 400
    assert "private prompt" not in caplog.text


def test_requires_authenticated_subject():
    request = event({"source": "agent", "failure": "network"})
    request["requestContext"] = {}
    assert module.handler(request, None)["statusCode"] == 401


def test_accepts_base64_encoded_diagnostic():
    request = event({"source": "agent", "failure": "network"})
    request["body"] = base64.b64encode(request["body"].encode()).decode()
    request["isBase64Encoded"] = True
    assert module.handler(request, None)["statusCode"] == 204


@pytest.mark.parametrize("encoded", [False, True])
def test_rejects_oversized_body_without_logging(encoded, caplog):
    request = event({"source": "agent", "failure": "network", "extra": "private" * 200})
    if encoded:
        request["body"] = base64.b64encode(request["body"].encode()).decode()
        request["isBase64Encoded"] = True
    assert module.handler(request, None)["statusCode"] == 400
    assert "private" not in caplog.text


def test_does_not_allow_untrusted_origin(monkeypatch):
    monkeypatch.setenv("ALLOWED_ORIGINS", "https://site.example")
    request = event({"source": "agent", "failure": "network"})
    request["headers"]["Origin"] = "https://untrusted.example"
    response = module.handler(request, None)
    assert "Access-Control-Allow-Origin" not in response["headers"]
