import importlib.util
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location("b99_interceptor", Path(__file__).with_name("interceptor.py"))
interceptor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(interceptor)


class Verifier:
    def __init__(self, claims):
        self.claims = claims

    def verify(self, token):
        if token != "verified":
            raise ValueError("bad signature")
        return self.claims


@pytest.fixture
def gateway_event(monkeypatch):
    monkeypatch.setenv("SPIKE_SCOPE", "cei-tools/src.pubmed")
    monkeypatch.setattr(interceptor, "verifier", lambda: Verifier({
        "cei_user_sub": "user-a", "cei_session_id": "session-a", "scope": "cei-tools/src.pubmed"
    }))
    return {"mcp": {"gatewayRequest": {"headers": {"Authorization": "Bearer verified"}, "body": {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "spike___echo",
        "arguments": {"query": "test", "_cei_spike_identity": {"user_id": "user-b"}}}}}}}


def test_overwrites_forged_identity_without_mutating_input(gateway_event):
    out = interceptor.handler(gateway_event, None)["mcp"]["transformedGatewayRequest"]["body"]
    assert out["params"]["arguments"]["_cei_spike_identity"] == {"user_id": "user-a", "session_id": "session-a"}
    assert gateway_event["mcp"]["gatewayRequest"]["body"]["params"]["arguments"]["_cei_spike_identity"]["user_id"] == "user-b"


@pytest.mark.parametrize("claims", [
    {"cei_user_sub": "user-a", "cei_session_id": "session-a", "scope": "cei-tools/src.other"},
    {"cei_user_sub": "../user-b", "cei_session_id": "session-a", "scope": "cei-tools/src.pubmed"},
    {"cei_user_sub": "user-a", "scope": "cei-tools/src.pubmed"},
])
def test_rejects_invalid_binding_or_scope(gateway_event, monkeypatch, claims):
    monkeypatch.setattr(interceptor, "verifier", lambda: Verifier(claims))
    assert interceptor.handler(gateway_event, None)["mcp"]["transformedGatewayResponse"]["statusCode"] == 403


def test_rejects_invalid_token(gateway_event):
    gateway_event["mcp"]["gatewayRequest"]["headers"]["Authorization"] = "Bearer forged"
    assert interceptor.handler(gateway_event, None)["mcp"]["transformedGatewayResponse"]["statusCode"] == 403


def test_no_identity_leaks_in_rejection(gateway_event):
    gateway_event["mcp"]["gatewayRequest"]["headers"] = {}
    assert "user-b" not in str(interceptor.handler(gateway_event, None))

