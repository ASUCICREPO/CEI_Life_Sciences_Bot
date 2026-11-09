import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from shared.runtime.router_adapters import CognitoTokenIssuer, RouterServiceError, RuntimeInvoker, session_owned


def test_session_ownership_is_user_keyed_and_consistent():
    table = Mock()
    table.get_item.return_value = {"Item": {"user_id": "alice"}}
    assert session_owned(table, "alice", "session")
    table.get_item.assert_called_once_with(Key={"user_id": "alice", "session_id": "session"}, ConsistentRead=True)


def test_token_issuer_refuses_non_cognito_endpoint():
    with pytest.raises(RouterServiceError):
        CognitoTokenIssuer(endpoint="https://attacker.example/oauth2/token", client_id="x", secret_arn="a", secrets_client=Mock())


def test_token_issuer_uses_trusted_metadata_and_does_not_redirect(monkeypatch):
    secrets = Mock()
    secrets.get_secret_value.return_value = {"SecretString": "client-secret"}
    response = Mock(status_code=200)
    response.json.return_value = {"access_token": "gateway-token", "scope": "cei-tools/src.pubmed"}
    post = Mock(return_value=response)
    monkeypatch.setattr("shared.runtime.router_adapters.requests.post", post)
    issuer = CognitoTokenIssuer(endpoint="https://cei.auth.us-east-2.amazoncognito.com/oauth2/token",
                                client_id="client", secret_arn="secret", secrets_client=secrets)
    result = asyncio.run(issuer("alice", "session", ("cei-tools/src.pubmed",)))
    assert result["access_token"] == "gateway-token"
    assert post.call_args.kwargs["allow_redirects"] is False
    assert '"cei_user_sub": "alice"' in post.call_args.kwargs["data"]["aws_client_metadata"]


def test_runtime_invoker_waits_for_consumer_and_closes_response(monkeypatch):
    response = Mock(status_code=200)
    post = Mock(return_value=response)
    monkeypatch.setattr("shared.runtime.router_adapters.requests.post", post)
    consume = AsyncMock(return_value="finished")
    invoker = RuntimeInvoker(url="https://bedrock-agentcore.us-east-2.amazonaws.com/runtimes/abc/invocations?qualifier=DEFAULT",
                             consume=consume)
    assert asyncio.run(invoker("user-token", "tool-token", "session", {"prompt": "hello"})) == "finished"
    assert post.call_args.kwargs["headers"]["X-Cei-Gateway-Token"] == "tool-token"
    assert post.call_args.kwargs["allow_redirects"] is False
    consume.assert_awaited_once_with(response)
    response.close.assert_called_once()
