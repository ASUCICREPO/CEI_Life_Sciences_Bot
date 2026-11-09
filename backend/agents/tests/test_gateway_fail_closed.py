from unittest.mock import Mock

import pytest

from shared.schemas.citation import SourceType
from shared.tools.base import GatewayUnavailableError
from shared.tools.data_sources.pubmed import PubMedTool
from research import nodes


@pytest.mark.parametrize("direct_mode", [True, False])
def test_deployed_source_never_bypasses_gateway(monkeypatch, direct_mode):
    monkeypatch.setenv("CEI_REQUIRE_TOOL_GATEWAY", "true")
    tool = PubMedTool(direct_mode=direct_mode)
    tool.fetch = Mock()
    with pytest.raises(GatewayUnavailableError):
        tool.invoke(query="test")
    tool.fetch.assert_not_called()


def test_research_does_not_suppress_gateway_unavailable(monkeypatch):
    tool = Mock(source_type=SourceType.PUBMED)
    tool.invoke.side_effect = GatewayUnavailableError("unavailable")
    monkeypatch.setattr(nodes, "ALL_DATA_SOURCE_TOOLS", [tool])
    with pytest.raises(GatewayUnavailableError):
        nodes.retrieve({"query": "test", "triage_plan": None})
