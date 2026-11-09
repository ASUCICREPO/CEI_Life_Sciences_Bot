import base64
import io
import json
import socket
import importlib.util
import sys
import urllib.error
from email.message import Message
from pathlib import Path

import pytest

# ADR: load by file path under a unique name | Rationale: every Lambda dir has a handler.py, and a bare
# `import handler` returns whichever one another test file cached first (pytest shares sys.modules)
_spec = importlib.util.spec_from_file_location("agent_proxy_handler", Path(__file__).resolve().parent.parent / "handler.py")
handler = importlib.util.module_from_spec(_spec)
sys.modules["agent_proxy_handler"] = handler
_spec.loader.exec_module(handler)

TARGET = "https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/arn%3Aaws%3Abedrock-agentcore%3Aus-east-1%3A123%3Aruntime%2Fr/invocations?qualifier=DEFAULT"
SESSION = "0f8b6c1e-2a4d-4e5f-9a7b-1c2d3e4f5a6b"


class _Ctx:
    aws_request_id = "req-1"


def _event(method="POST", path="/research", headers=None, body='{"op":"run"}', b64=False):
    base = {"authorization": "Bearer abc.def.ghi", handler.SESSION_HEADER.lower(): SESSION}
    if headers is not None:
        base = headers
    return {
        "rawPath": path,
        "requestContext": {"http": {"method": method}},
        "headers": base,
        "body": base64.b64encode(body.encode()).decode() if b64 and body else body,
        "isBase64Encoded": b64,
    }


class _Upstream:
    def __init__(self, status=200, body=b'{"text":"hi","status":"completed"}', ctype="application/json"):
        self.status = status
        self._body = body
        msg = Message()
        msg["content-type"] = ctype
        self.headers = msg

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AGENT_URLS", json.dumps({"research": TARGET}))


@pytest.fixture
def captured(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append((request, timeout))
        return _Upstream()

    monkeypatch.setattr(handler.urllib.request, "urlopen", fake_urlopen)
    return calls


def _error_code(result):
    return json.loads(result["body"])["error"]["code"]


def test_forwards_post_with_callers_token_and_session(captured):
    result = handler.handler(_event(), _Ctx())
    assert result["statusCode"] == 200
    assert json.loads(result["body"]) == {"text": "hi", "status": "completed"}
    request, timeout = captured[0]
    assert request.full_url == TARGET
    assert request.get_method() == "POST"
    assert request.data == b'{"op":"run"}'
    assert request.get_header("Authorization") == "Bearer abc.def.ghi"
    # urllib capitalises only the first letter of stored header names
    assert request.get_header(handler.SESSION_HEADER.capitalize()) == SESSION
    assert timeout == pytest.approx(870.0)


def test_never_adds_cors_headers_itself(captured):
    # The function URL adds CORS headers; duplicates would make the browser reject the response.
    result = handler.handler(_event(), _Ctx())
    assert not any(h.lower().startswith("access-control-") for h in result["headers"])


def test_forwards_lambda_trace_context(captured, monkeypatch):
    trace = "Root=1-5759e988-bd862e3fe1be46a994272793;Parent=53995c3f42cd8ad8;Sampled=1"
    monkeypatch.setenv("_X_AMZN_TRACE_ID", trace)
    handler.handler(_event(), _Ctx())
    assert captured[0][0].get_header("X-amzn-trace-id") == trace


def test_base64_body_is_decoded(captured):
    handler.handler(_event(b64=True), _Ctx())
    assert captured[0][0].data == b'{"op":"run"}'


def test_options_returns_204_without_upstream_call(captured):
    assert handler.handler(_event(method="OPTIONS"), _Ctx())["statusCode"] == 204
    assert captured == []


def test_non_post_rejected(captured):
    result = handler.handler(_event(method="GET"), _Ctx())
    assert result["statusCode"] == 405
    assert captured == []


@pytest.mark.parametrize("path", ["/unknown", "/", "", "/research/extra", "/../research"])
def test_unknown_agent_is_404(captured, path):
    result = handler.handler(_event(path=path), _Ctx())
    assert result["statusCode"] == 404
    assert captured == []


@pytest.mark.parametrize(
    "auth",
    [None, "", "Bearer", "Bearer ", "Basic abc", "abc.def.ghi", "Bearer " + "a" * 9000, "Bearer tökén"],
)
def test_missing_or_malformed_bearer_is_401(captured, auth):
    headers = {handler.SESSION_HEADER.lower(): SESSION}
    if auth is not None:
        headers["authorization"] = auth
    result = handler.handler(_event(headers=headers), _Ctx())
    assert result["statusCode"] == 401
    assert _error_code(result) == "unauthorized"
    assert captured == []


@pytest.mark.parametrize(
    "session",
    [
        None, "", "short", "a" * 257, SESSION + "\r\nX: y", "bad id with spaces.........",
        "default", "a" * 36, "0b0c4f7e11114a118a11000000000001",  # no default ids, UUIDs only
    ],
)
def test_invalid_session_id_is_400(captured, session):
    headers = {"authorization": "Bearer abc"}
    if session is not None:
        headers[handler.SESSION_HEADER.lower()] = session
    result = handler.handler(_event(headers=headers), _Ctx())
    assert result["statusCode"] == 400
    assert captured == []


def test_missing_body_is_400(captured):
    assert handler.handler(_event(body=None), _Ctx())["statusCode"] == 400
    assert captured == []


def test_oversized_body_is_413(captured):
    result = handler.handler(_event(body="x" * (handler._MAX_BODY_BYTES + 1)), _Ctx())
    assert result["statusCode"] == 413
    assert captured == []


def test_upstream_http_error_passes_status_and_body(monkeypatch):
    def fake_urlopen(request, timeout):
        msg = Message()
        msg["content-type"] = "application/json"
        raise urllib.error.HTTPError(TARGET, 401, "Unauthorized", msg, io.BytesIO(b'{"message":"bad token"}'))

    monkeypatch.setattr(handler.urllib.request, "urlopen", fake_urlopen)
    result = handler.handler(_event(), _Ctx())
    assert result["statusCode"] == 401
    assert json.loads(result["body"]) == {"message": "bad token"}


def test_upstream_http_error_without_body_gets_envelope(monkeypatch):
    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(TARGET, 429, "Too Many", Message(), io.BytesIO(b""))

    monkeypatch.setattr(handler.urllib.request, "urlopen", fake_urlopen)
    result = handler.handler(_event(), _Ctx())
    assert result["statusCode"] == 429
    assert json.loads(result["body"])["error"]["retriable"] is True


@pytest.mark.parametrize("exc", [socket.timeout("t"), TimeoutError("t"), urllib.error.URLError(socket.timeout("t"))])
def test_upstream_timeout_is_504(monkeypatch, exc):
    def fake_urlopen(request, timeout):
        raise exc

    monkeypatch.setattr(handler.urllib.request, "urlopen", fake_urlopen)
    assert handler.handler(_event(), _Ctx())["statusCode"] == 504


def test_unreachable_upstream_is_502(monkeypatch):
    def fake_urlopen(request, timeout):
        raise urllib.error.URLError("dns failure")

    monkeypatch.setattr(handler.urllib.request, "urlopen", fake_urlopen)
    assert handler.handler(_event(), _Ctx())["statusCode"] == 502


def test_non_https_agent_url_is_ignored(monkeypatch, captured):
    monkeypatch.setenv("AGENT_URLS", json.dumps({"research": "http://evil.example/"}))
    assert handler.handler(_event(), _Ctx())["statusCode"] == 404
    assert captured == []


def _session_event(session):
    headers = {"authorization": "Bearer abc.def.ghi"}
    if session is not None:
        headers[handler.SESSION_HEADER.lower()] = session
    return _event(headers=headers)


@pytest.mark.parametrize("session", [None, "session-1", "default"], ids=["missing", "session-1", "default"])
def test_missing_or_non_uuid_session_header_is_400_without_upstream_call(captured, session):
    result = handler.handler(_session_event(session), _Ctx())
    assert result["statusCode"] == 400
    assert _error_code(result) == "invalid_request"
    assert captured == []


def test_uppercase_uuid_is_accepted_and_forwarded_verbatim(captured):
    upper = SESSION.upper()
    result = handler.handler(_session_event(upper), _Ctx())
    assert result["statusCode"] == 200
    assert captured[0][0].get_header(handler.SESSION_HEADER.capitalize()) == upper  # not lower-cased


def test_braced_uuid_is_rejected_400(captured):
    result = handler.handler(_session_event("{" + SESSION + "}"), _Ctx())
    assert result["statusCode"] == 400
    assert captured == []


def test_uuid_with_trailing_newline_is_rejected_400(captured):
    # `$` in a Python regex also matches just before a trailing "\n"; the header must still be refused.
    result = handler.handler(_session_event(SESSION + "\n"), _Ctx())
    assert result["statusCode"] == 400
    assert captured == []


def test_valid_uuid_forwarded_unchanged_and_no_id_generated(captured, monkeypatch):
    import uuid

    def _no_generation(*_a, **_k):
        raise AssertionError("proxy must never generate a session id")

    for name in ("uuid1", "uuid4"):
        monkeypatch.setattr(uuid, name, _no_generation)
    result = handler.handler(_session_event(SESSION), _Ctx())
    assert result["statusCode"] == 200
    assert len(captured) == 1
    assert captured[0][0].get_header(handler.SESSION_HEADER.capitalize()) == SESSION


def test_logs_never_contain_token_or_session(captured, caplog):
    caplog.set_level("INFO")
    handler.handler(_event(), _Ctx())
    text = caplog.text
    assert "abc.def.ghi" not in text
    assert SESSION not in text


def test_unexpected_error_log_does_not_include_exception_message(monkeypatch, caplog):
    def fail(*args, **kwargs):
        raise RuntimeError("secret request detail")
    monkeypatch.setattr(handler.urllib.request, "urlopen", fail)
    assert handler.handler(_event(), _Ctx())["statusCode"] == 502
    assert "RuntimeError" in caplog.text
    assert "secret request detail" not in caplog.text


def test_success_http_error_envelope_is_logged_without_body(monkeypatch, caplog):
    monkeypatch.setattr(handler.urllib.request, "urlopen", lambda *_args, **_kwargs: _Upstream(body=b'{"status":"error","message":"private response"}'))
    result = handler.handler(_event(), _Ctx())
    assert result["statusCode"] == 200
    assert json.loads(result["body"])["message"] == "private response"
    assert "agent_proxy_application_error" in caplog.text
    assert "private response" not in caplog.text


def test_invalid_upstream_json_is_logged_without_body(monkeypatch, caplog):
    monkeypatch.setattr(handler.urllib.request, "urlopen", lambda *_args, **_kwargs: _Upstream(body=b'{private invalid json'))
    result = handler.handler(_event(), _Ctx())
    assert result["statusCode"] == 200
    assert result["body"] == "{private invalid json"
    assert "agent_proxy_invalid_json" in caplog.text
    assert "private invalid json" not in caplog.text


def test_deep_upstream_json_still_passes_through(monkeypatch, caplog):
    body = b'[' * 2000 + b'0' + b']' * 2000
    original_loads = json.loads
    def loads(value, *args, **kwargs):
        if isinstance(value, bytes):
            raise RecursionError("private response")
        return original_loads(value, *args, **kwargs)
    monkeypatch.setattr(handler.json, "loads", loads)
    monkeypatch.setattr(handler.urllib.request, "urlopen", lambda *_args, **_kwargs: _Upstream(body=body))
    result = handler.handler(_event(), _Ctx())
    assert result["statusCode"] == 200
    assert result["body"] == body.decode()
    assert "agent_proxy_invalid_json" in caplog.text
