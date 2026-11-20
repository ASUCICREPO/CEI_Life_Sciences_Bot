import base64
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import http_errors as he

ORIGIN = "https://site.example.net"


def _event(rid="req-123", origin=None, claims=None):
    ctx = {"requestId": rid}
    if claims is not None:
        ctx["authorizer"] = {"claims": claims}
    event = {"requestContext": ctx, "headers": {}}
    if origin:
        event["headers"]["Origin"] = origin
    return event


def _raw_cursor(obj):
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")


# --- api_handler ---------------------------------------------------------------


def test_api_handler_unexpected_error_returns_generic_500_and_logs():
    logger = Mock()

    @he.api_handler(logger)
    def route(event, context):
        raise RuntimeError("secret detail")

    resp = route(_event(rid="req-abc"), None)

    assert resp["statusCode"] == 500
    body = json.loads(resp["body"])
    assert body["error"]["code"] == "internal_error"
    assert body["error"]["message"] == he.GENERIC_INTERNAL_ERROR
    assert body["error"]["request_id"] == "req-abc"
    assert body["error"]["retriable"] is False
    assert "secret detail" not in resp["body"]
    logger.exception.assert_called_once()
    assert logger.exception.call_args.kwargs["extra"] == {"request_id": "req-abc"}


def test_api_handler_logs_traceback_via_real_logger(caplog):
    @he.api_handler(__import__("logging").getLogger("test_http_errors"))
    def route(event, context):
        raise RuntimeError("secret detail")

    with caplog.at_level("ERROR", logger="test_http_errors"):
        resp = route({}, SimpleNamespace(aws_request_id="ctx-rid"))

    assert json.loads(resp["body"])["error"]["request_id"] == "ctx-rid"
    rec = caplog.records[-1]
    assert rec.request_id == "ctx-rid"
    assert rec.exc_info and "secret detail" in str(rec.exc_info[1])


def test_json_formatter_redacts_exception_and_chained_messages(caplog):
    import logging

    try:
        try:
            raise ValueError("secret cause")
        except ValueError as exc:
            raise RuntimeError("secret detail") from exc
    except RuntimeError:
        logging.getLogger("test_safe_format").exception("request failed", extra={"request_id": "req-1"})
    payload = json.loads(he.JsonFormatter().format(caplog.records[-1]))
    assert payload["exception_type"] == "RuntimeError"
    assert payload["cause_type"] == "ValueError"
    assert payload["frames"][-1]["function"] == "test_json_formatter_redacts_exception_and_chained_messages"
    assert "secret" not in json.dumps(payload)


def test_api_handler_api_error_passes_through_status_and_code():
    logger = Mock()

    @he.api_handler(logger)
    def route(event, context):
        raise he.ApiError(409, "stale_revision", "Revision is stale.", retriable=True)

    resp = route(_event(), None)

    assert resp["statusCode"] == 409
    err = json.loads(resp["body"])["error"]
    assert err == {"code": "stale_revision", "message": "Revision is stale.", "request_id": "req-123", "retriable": True}
    logger.exception.assert_not_called()


def test_api_handler_logs_handled_server_errors_without_message():
    logger = Mock()

    @he.api_handler(logger)
    def route(event, context):
        raise he.ApiError(503, "service_unavailable", "sensitive detail")

    assert route(_event(), None)["statusCode"] == 503
    logger.error.assert_called_once_with("handled_api_failure", extra={"request_id": "req-123", "route": None,
                                                                      "status": 503, "code": "service_unavailable"})


def test_api_handler_not_found_helper_is_404():
    @he.api_handler(Mock())
    def route(event, context):
        raise he.not_found()

    resp = route(_event(), None)
    assert resp["statusCode"] == 404
    assert json.loads(resp["body"])["error"]["code"] == "not_found"


def test_api_handler_returns_success_untouched():
    @he.api_handler(Mock())
    def route(event, context):
        return he.json_response(200, {"ok": True}, event)

    assert route(_event(), None)["statusCode"] == 200


def test_request_id_falls_back_to_unknown():
    assert he.request_id({}, None) == "req_unknown"


# --- cursors -----------------------------------------------------------------

EXPECTED = {"user_id": lambda v: v == "u1", "session_id": lambda v: isinstance(v, str)}


def test_decode_cursor_none_for_missing():
    assert he.decode_cursor(None, EXPECTED) is None
    assert he.decode_cursor("", EXPECTED) is None


def test_decode_cursor_roundtrip_valid():
    key = {"user_id": "u1", "session_id": "s1"}
    assert he.decode_cursor(he.encode_cursor(key), EXPECTED) == key


@pytest.mark.parametrize(
    "key",
    [
        {"user_id": "u1"},  # missing key
        {"user_id": "u1", "session_id": "s1", "extra": "x"},  # extra key
    ],
)
def test_decode_cursor_rejects_key_set_mismatch(key):
    with pytest.raises(he.ApiError) as ei:
        he.decode_cursor(_raw_cursor(key), EXPECTED)
    assert ei.value.status == 400 and ei.value.code == "invalid_request"


def test_decode_cursor_rejects_failing_predicate():
    with pytest.raises(he.ApiError) as ei:
        he.decode_cursor(_raw_cursor({"user_id": "other-user", "session_id": "s1"}), EXPECTED)
    assert ei.value.status == 400


@pytest.mark.parametrize("cursor", ["!!!not-base64!!!", _raw_cursor(["a", "b"]), "bm90IGpzb24"])
def test_decode_cursor_rejects_garbage(cursor):
    with pytest.raises(he.ApiError) as ei:
        he.decode_cursor(cursor, EXPECTED)
    assert ei.value.code == "invalid_request"


def test_page_envelope_has_more_flags():
    assert he.page_envelope([], None) == {"items": [], "next_cursor": None, "has_more": False}
    env = he.page_envelope([1], {"user_id": "u1", "session_id": "s1"})
    assert env["has_more"] is True and env["next_cursor"]


# --- parse_limit -------------------------------------------------------------


@pytest.mark.parametrize("params", [None, {}, {"limit": ""}])
def test_parse_limit_default(params):
    assert he.parse_limit(params) == he.DEFAULT_LIMIT == 20


def test_parse_limit_custom_default():
    assert he.parse_limit({}, default=5) == 5


@pytest.mark.parametrize("raw,expected", [("1", 1), ("50", 50), (str(he.MAX_LIMIT), he.MAX_LIMIT)])
def test_parse_limit_accepts_bounds(raw, expected):
    assert he.parse_limit({"limit": raw}) == expected


@pytest.mark.parametrize("raw", ["0", "-1", str(he.MAX_LIMIT + 1), "abc", "1.5"])
def test_parse_limit_rejects_out_of_range_or_non_int(raw):
    with pytest.raises(he.ApiError) as ei:
        he.parse_limit({"limit": raw})
    assert ei.value.status == 400 and ei.value.code == "invalid_request"


# --- user_id_from_claims -----------------------------------------------------


@pytest.mark.parametrize(
    "event",
    [
        {},
        {"requestContext": {}},
        {"requestContext": {"authorizer": {}}},
        {"requestContext": {"authorizer": {"claims": {}}}},
        {"requestContext": {"authorizer": {"claims": {"sub": ""}}}},
        {"requestContext": {"authorizer": {"claims": {"sub": 123}}}},
    ],
)
def test_user_id_from_claims_401_when_missing(event):
    with pytest.raises(he.ApiError) as ei:
        he.user_id_from_claims(event)
    assert ei.value.status == 401 and ei.value.code == "unauthorized"


def test_user_id_from_claims_returns_sub():
    assert he.user_id_from_claims(_event(claims={"sub": "user-1"})) == "user-1"


# --- require_age_verified (B112) ----------------------------------------------


def test_require_age_verified_passes_with_the_claim():
    he.require_age_verified(_event(claims={"sub": "user-1", "cei_age_verified": "true"}))


@pytest.mark.parametrize(
    "claims",
    [
        {"sub": "user-1"},
        {"sub": "user-1", "cei_age_verified": "false"},
        {"sub": "user-1", "cei_age_verified": True},
        {},
    ],
)
def test_require_age_verified_rejects_missing_or_false_claim(claims):
    with pytest.raises(he.ApiError) as ei:
        he.require_age_verified(_event(claims=claims))
    assert ei.value.status == 403 and ei.value.code == "onboarding_required"


# --- CORS --------------------------------------------------------------------


def test_cors_echoes_allowlisted_origin(monkeypatch):
    monkeypatch.setenv("ALLOWED_ORIGINS", f" https://other.example , {ORIGIN} ")
    assert he.cors_headers(_event(origin=ORIGIN)) == {"Access-Control-Allow-Origin": ORIGIN, "Vary": "Origin"}


def test_cors_header_lookup_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("ALLOWED_ORIGINS", ORIGIN)
    assert he.cors_headers({"headers": {"origin": ORIGIN}})["Access-Control-Allow-Origin"] == ORIGIN


def test_cors_rejects_unlisted_origin(monkeypatch):
    monkeypatch.setenv("ALLOWED_ORIGINS", ORIGIN)
    assert he.cors_headers(_event(origin="https://evil.example")) == {}


def test_cors_empty_when_no_allowlist_or_no_origin(monkeypatch):
    monkeypatch.delenv("ALLOWED_ORIGINS", raising=False)
    assert he.cors_headers(_event(origin=ORIGIN)) == {}
    monkeypatch.setenv("ALLOWED_ORIGINS", ORIGIN)
    assert he.cors_headers(_event()) == {}
    assert he.cors_headers({"headers": None}) == {}


def test_error_response_carries_cors_for_allowlisted_origin(monkeypatch):
    monkeypatch.setenv("ALLOWED_ORIGINS", ORIGIN)

    @he.api_handler(Mock())
    def route(event, context):
        raise RuntimeError("boom")

    resp = route(_event(origin=ORIGIN), None)
    assert resp["headers"]["Access-Control-Allow-Origin"] == ORIGIN
    assert "*" not in resp["headers"].values()
