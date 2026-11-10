import importlib.util
import json
import sys
import urllib.error
from pathlib import Path

import pytest

LAMBDAS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(LAMBDAS / "shared"))  # http_errors (shipped in the python-deps layer)

# ADR: load by file path under a unique name | Rationale: several Lambdas have a top-level handler.py
_spec = importlib.util.spec_from_file_location(
    "token_refresh_handler", LAMBDAS / "token-refresh" / "handler.py"
)
refresh = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = refresh  # dataclasses resolve annotations through sys.modules
_spec.loader.exec_module(refresh)

ACCOUNT = "123456789012"
RUNTIME_ROLE = f"arn:aws:iam::{ACCOUNT}:role/cei-agent-runtime"
CALLER = f"arn:aws:sts::{ACCOUNT}:assumed-role/cei-agent-runtime/session-1"
SUB = "3b1f6c1e-1d2a-4c5b-9e8f-0123456789ab"
SCOPES = ("cei-tools/common", "cei-tools/src.pubmed")
ALL_SCOPES = "cei-tools/src.pubmed,cei-tools/src.chembl,cei-tools/common"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("ALLOWED_CALLER_ROLE_ARNS", RUNTIME_ROLE)
    monkeypatch.setenv("ALLOWED_SCOPES", ALL_SCOPES)
    monkeypatch.setenv("ROUTER_CLIENT_ID", "routerclient")
    monkeypatch.setenv(
        "ROUTER_SECRET_ARN", "arn:aws:secretsmanager:us-east-2:123456789012:secret:x"
    )
    monkeypatch.setenv(
        "TOKEN_ENDPOINT", "https://cei-ls.auth.us-east-2.amazoncognito.com/oauth2/token"
    )
    monkeypatch.setattr(refresh, "_user_verifier", lambda token: SUB)
    monkeypatch.setattr(refresh, "gateway_verifier", lambda: type("Verifier", (), {
        "verify": lambda self, token: {"scope": " ".join(SCOPES), "cei_user_sub": SUB, "cei_session_id": "s-1"}
    })())


@pytest.fixture
def active(monkeypatch):
    """Make session s-1 an active run and record every token request."""
    minted: list = []

    def lookup(user_id, session_id, run_id, job_id):
        return (
            refresh.ActiveRun(user_sub=SUB, session_id=session_id, scopes=SCOPES)
            if session_id == "s-1"
            else None
        )

    def mint(run):
        minted.append(run)
        return {"access_token": "new.jwt.token", "expires_in": 3600}

    monkeypatch.setattr(refresh, "_run_lookup", lookup)
    monkeypatch.setattr(refresh, "_token_requester", mint)
    return minted


def event(body, caller=CALLER, method="POST", resource="/internal/token-refresh"):
    if body and "session_id" in body:
        body = {"run_id": "run-1", "user_token": "user-jwt", **body}
    return {
        "httpMethod": method,
        "resource": resource,
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {"requestId": "req-1", "identity": {"userArn": caller}},
        "headers": {},
    }


def call(evt):
    resp = refresh.handler(evt, None)
    return resp["statusCode"], json.loads(resp["body"])


def test_active_run_gets_a_same_scope_token(active):
    status, body = call(event({"session_id": "s-1"}))
    assert status == 200
    assert body == {"access_token": "new.jwt.token", "expires_in": 3600, "scopes": sorted(SCOPES)}
    assert active[0].user_sub == SUB and active[0].scopes == SCOPES


def test_inactive_run_is_refused(active):
    status, body = call(event({"session_id": "s-2"}))
    assert status == 404 and body["error"]["code"] == "not_found"
    assert active == []


def test_invalid_user_token_is_refused_before_run_lookup(active, monkeypatch):
    def reject(_token):
        raise refresh.UnauthenticatedError("invalid")
    monkeypatch.setattr(refresh, "_user_verifier", reject)
    status, body = call(event({"session_id": "s-1"}))
    assert status == 401 and body["error"]["code"] == "unauthorized"
    assert active == []


def test_verified_user_and_run_reach_lookup(monkeypatch):
    calls = []
    monkeypatch.setattr(refresh, "_run_lookup", lambda u, s, r, j: calls.append((u, s, r, j)))
    status, _ = call(event({"session_id": "s-1", "run_id": "run-2"}))
    assert status == 404 and calls == [(SUB, "s-1", "run-2", None)]


def test_f4_default_fails_closed(monkeypatch):
    """No active-run record exists in F4, so even an allowed caller cannot refresh."""
    minted = []
    monkeypatch.setattr(refresh, "_token_requester", lambda run: minted.append(run))
    status, body = call(event({"session_id": "s-1"}))
    assert status == 404 and minted == []


@pytest.mark.parametrize(
    "caller",
    [
        f"arn:aws:sts::{ACCOUNT}:assumed-role/some-other-role/s",
        f"arn:aws:sts::999999999999:assumed-role/cei-agent-runtime/s",  # same name, other account
        f"arn:aws:iam::{ACCOUNT}:user/admin",
        "",
    ],
)
def test_callers_outside_the_allowlist_get_403(active, caller):
    status, body = call(event({"session_id": "s-1"}, caller=caller))
    assert status == 403 and body["error"]["code"] == "forbidden"
    assert active == []


def test_empty_allowlist_refuses_everyone(active, monkeypatch):
    monkeypatch.setenv("ALLOWED_CALLER_ROLE_ARNS", "")
    assert call(event({"session_id": "s-1"}))[0] == 403


def test_role_with_path_is_matched_by_name(active, monkeypatch):
    monkeypatch.setenv(
        "ALLOWED_CALLER_ROLE_ARNS", f"arn:aws:iam::{ACCOUNT}:role/service-role/cei-agent-runtime"
    )
    assert call(event({"session_id": "s-1"}))[0] == 200


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"session_id": "../x"},
        {"session_id": "s-1", "job_id": 5},
        {"session_id": "s-1", "scopes": ["admin"]},
    ],
)
def test_malformed_requests_are_400(active, body):
    status, resp = call(event(body))
    assert status == 400 and resp["error"]["code"] == "invalid_request"


def test_caller_cannot_choose_scopes(active):
    """Scopes always come from the run record; a `scopes` field is rejected outright."""
    status, _ = call(event({"session_id": "s-1", "scopes": ["cei-tools/src.chembl"]}))
    assert status == 400 and active == []


def test_run_scopes_outside_the_allowed_set_are_refused(monkeypatch):
    monkeypatch.setattr(
        refresh, "_run_lookup", lambda u, s, r, j: refresh.ActiveRun(SUB, s, ("cei-tools/admin",))
    )
    monkeypatch.setattr(refresh, "_token_requester", lambda run: pytest.fail("must not mint"))
    assert call(event({"session_id": "s-1"}))[0] == 500


def test_token_endpoint_failure_is_retriable_503(active, monkeypatch):
    def boom(run):
        raise urllib.error.HTTPError("https://x", 500, "err", None, None)

    monkeypatch.setattr(refresh, "_token_requester", boom)
    status, body = call(event({"session_id": "s-1"}))
    assert status == 503 and body["error"]["retriable"] is True


def test_narrower_granted_scope_is_refused(active, monkeypatch):
    monkeypatch.setattr(refresh, "gateway_verifier", lambda: type("Verifier", (), {
        "verify": lambda self, token: {"scope": "cei-tools/common", "cei_user_sub": SUB, "cei_session_id": "s-1"}
    })())
    assert call(event({"session_id": "s-1"}))[0] == 503


def test_wrong_route_is_404(active):
    assert call(event({"session_id": "s-1"}, resource="/sessions"))[0] == 404


def test_request_token_sends_scopes_and_client_metadata(monkeypatch):
    sent = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"access_token": "t", "expires_in": 3600, "scope": "cei-tools/common cei-tools/src.pubmed"}'

    def fake_urlopen(req, timeout):
        sent["url"], sent["headers"], sent["data"] = (
            req.full_url,
            dict(req.headers),
            req.data.decode(),
        )
        return Resp()

    monkeypatch.setattr(refresh, "_client_secret", "s3cret")
    monkeypatch.setattr(refresh.urllib.request, "urlopen", fake_urlopen)
    refresh.request_token(refresh.ActiveRun(SUB, "s-1", SCOPES))
    form = dict(refresh.urllib.parse.parse_qsl(sent["data"]))
    assert form["grant_type"] == "client_credentials"
    assert form["scope"] == "cei-tools/common cei-tools/src.pubmed"
    assert json.loads(form["aws_client_metadata"]) == {"cei_user_sub": SUB, "cei_session_id": "s-1"}
    assert sent["headers"]["Authorization"].startswith("Basic ")


def test_logs_never_contain_the_token(active, caplog):
    caplog.set_level("INFO")
    call(event({"session_id": "s-1"}))
    assert "new.jwt.token" not in caplog.text
