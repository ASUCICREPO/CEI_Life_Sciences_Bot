from unittest.mock import Mock
import json

import pytest

from shared.tools.gateway_mcp import GatewayCallError, GatewayDeniedError, call_gateway_tool


def test_successful_scoped_tool_call(monkeypatch):
    response = Mock(status_code=200, text='{"jsonrpc":"2.0","id":"call","result":{"content":[{"type":"text","text":"{\\"results\\":[]}"}]}}')
    post = Mock(return_value=response)
    monkeypatch.setattr("shared.tools.gateway_mcp.requests.post", post)
    assert call_gateway_tool("https://abc.gateway.bedrock-agentcore.us-east-2.amazonaws.com/mcp", "secret-token", "pubmed", {"query": "x"}) == {"results": []}
    assert post.call_args.kwargs["json"]["params"]["name"] == "pubmed___search"
    assert post.call_args.kwargs["allow_redirects"] is False


def test_named_ncbi_target_and_rejected_separator(monkeypatch):
    response = Mock(status_code=200, text=json.dumps({"jsonrpc": "2.0", "id": "call", "result": {"structuredContent": {"result": {}}}}))
    post = Mock(return_value=response)
    monkeypatch.setattr("shared.tools.gateway_mcp.requests.post", post)
    url = "https://abc.gateway.bedrock-agentcore.us-east-2.amazonaws.com/mcp"
    call_gateway_tool(url, "secret-token", "ncbi-taxonomy", {"taxon": "human"}, tool_name="lineage")
    assert post.call_args.kwargs["json"]["params"]["name"] == "ncbi-taxonomy___lineage"
    with pytest.raises(GatewayCallError):
        call_gateway_tool(url, "secret-token", "ncbi___other", {}, tool_name="lineage")
    assert post.call_count == 1


def test_target_error_is_redacted(monkeypatch):
    payload = {"error": {"message": "private detail"}}
    response = Mock(status_code=200, text=json.dumps({"jsonrpc": "2.0", "id": "call", "result": {"structuredContent": payload}}))
    monkeypatch.setattr("shared.tools.gateway_mcp.requests.post", Mock(return_value=response))
    with pytest.raises(GatewayCallError) as exc:
        call_gateway_tool("https://abc.gateway.bedrock-agentcore.us-east-2.amazonaws.com/mcp", "secret", "ncbi-taxonomy", {}, tool_name="lineage")
    assert "private detail" not in str(exc.value)


def test_policy_denial_is_not_a_success(monkeypatch):
    response = Mock(status_code=200, text='{"jsonrpc":"2.0","id":"call","error":{"code":-32002,"message":"sensitive detail"}}')
    monkeypatch.setattr("shared.tools.gateway_mcp.requests.post", Mock(return_value=response))
    with pytest.raises(GatewayDeniedError, match="denied") as exc:
        call_gateway_tool("https://abc.gateway.bedrock-agentcore.us-east-2.amazonaws.com/mcp", "secret-token", "pubmed", {})
    assert "sensitive detail" not in str(exc.value) and "secret-token" not in str(exc.value)


def test_all_upstreams_failing_is_not_a_success(monkeypatch):
    payload = {"results": [], "query_metadata": {"errors": ["upstream failure"]}}
    response = Mock(status_code=200, text=json.dumps({"jsonrpc": "2.0", "id": "call", "result": {"structuredContent": payload}}))
    monkeypatch.setattr("shared.tools.gateway_mcp.requests.post", Mock(return_value=response))
    with pytest.raises(GatewayCallError, match="could not complete"):
        call_gateway_tool("https://abc.gateway.bedrock-agentcore.us-east-2.amazonaws.com/mcp", "secret-token", "openfda", {})


def test_non_https_endpoint_rejected():
    with pytest.raises(GatewayCallError):
        call_gateway_tool("http://gateway.example/mcp", "secret-token", "pubmed", {})


def test_attacker_https_endpoint_rejected(monkeypatch):
    post = Mock()
    monkeypatch.setattr("shared.tools.gateway_mcp.requests.post", post)
    with pytest.raises(GatewayCallError):
        call_gateway_tool("https://attacker.example/mcp", "secret-token", "pubmed", {})
    post.assert_not_called()
