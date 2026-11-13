from unittest.mock import Mock

import pytest

from shared.runtime.identity import UnauthenticatedError
from shared.tools.gateway_context import current_gateway_token, gateway_request_context
from shared.tools.base import GatewayUnavailableError
from shared.tools.data_sources.pubmed import PubMedTool
from research.nodes import _source_query_args, estimate_research_duration_seconds
from research.state import TriagePlan
from shared.schemas.citation import SourceType


def context(token="secret"):
    return Mock(request=Mock(headers={"X-Cei-Gateway-Token": token}))


def test_verified_binding_is_scoped_and_cleaned(monkeypatch):
    monkeypatch.setenv("CEI_REQUIRE_TOOL_GATEWAY", "true")
    verifier = Mock()
    verifier.verify.return_value = {"cei_user_sub": "alice", "cei_session_id": "session"}
    monkeypatch.setattr("shared.tools.gateway_context.gateway_verifier", lambda: verifier)
    with gateway_request_context(context(), "alice", "session"):
        assert current_gateway_token() == "secret"
    assert current_gateway_token() is None


@pytest.mark.parametrize("user,session", [("mallory", "session"), ("alice", "other")])
def test_token_cannot_cross_user_or_session(monkeypatch, user, session):
    monkeypatch.setenv("CEI_REQUIRE_TOOL_GATEWAY", "true")
    verifier = Mock()
    verifier.verify.return_value = {"cei_user_sub": "alice", "cei_session_id": "session"}
    monkeypatch.setattr("shared.tools.gateway_context.gateway_verifier", lambda: verifier)
    with pytest.raises(UnauthenticatedError), gateway_request_context(context(), user, session):
        pass
    assert current_gateway_token() is None


def test_gateway_transport_is_used_only_during_valid_context(monkeypatch):
    monkeypatch.setenv("CEI_REQUIRE_TOOL_GATEWAY", "true")
    monkeypatch.setenv("CEI_TOOL_GATEWAY_URL", "https://example.invalid/mcp")
    verifier = Mock()
    verifier.verify.return_value = {"cei_user_sub": "alice", "cei_session_id": "session"}
    monkeypatch.setattr("shared.tools.gateway_context.gateway_verifier", lambda: verifier)
    call = Mock(return_value={"source_type": "pubmed", "results": [], "citations": [], "query_metadata": {}})
    monkeypatch.setattr("shared.tools.gateway_mcp.call_gateway_tool", call)
    tool = PubMedTool(direct_mode=True)
    tool.fetch = Mock()
    with pytest.raises(GatewayUnavailableError):
        tool.invoke(query="test")
    with gateway_request_context(context(), "alice", "session"):
        assert tool.invoke(query="test").results == []
    tool.fetch.assert_not_called()
    assert call.call_args.args[:3] == ("https://example.invalid/mcp", "secret", "pubmed")


def test_source_query_fields_and_long_run_estimate():
    assert _source_query_args(SourceType.OPEN_TARGETS, "BRCA1") == {"target": "BRCA1"}
    assert _source_query_args(SourceType.NPI_REGISTRY, "Smith") == {"name": "Smith"}
    plan = TriagePlan(selected_sources=list(SourceType), source_queries={}, reasoning="many sources")
    assert estimate_research_duration_seconds(plan) > 600
    short = TriagePlan(selected_sources=[SourceType.PUBMED], source_queries={}, reasoning="one source")
    assert estimate_research_duration_seconds(short) <= 600
