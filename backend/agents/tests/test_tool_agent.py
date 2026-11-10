from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from shared.runtime.identity import UnauthenticatedError
from shared.tools.data_sources import TOOLS_BY_SOURCE_ID
from tool_agent.graph import SourceUnavailableError, compile_graph, source_tool
from tool_agent.main import authorize_grant, source_thread
from tool_agent.profile import SOURCE_IDS, load_profile
from tool_agent.ncbi_tools import ncbi_tools


class FakeTool:
    def __init__(self, name):
        self.name = name
        self.calls = []

    async def ainvoke(self, arguments):
        self.calls.append(arguments)
        return {"results": [{"title": "A record"}]}


class FakeModel:
    def __init__(self, name):
        self.name = name
        self.calls = 0

    def bind_tools(self, tools):
        assert len(tools) == 1 and tools[0].name == self.name
        return self

    async def ainvoke(self, messages):
        self.calls += 1
        if self.calls == 1:
            return AIMessage(content="", tool_calls=[
                {"name": "other_source", "args": {"query": "x"}, "id": "bad"},
                {"name": self.name, "args": {"query": "x"}, "id": "good"},
            ])
        return AIMessage(content="Found a record.")


def test_profiles_cover_approved_sources_and_reject_path_traversal():
    assert len(SOURCE_IDS) == 13
    assert SOURCE_IDS == set(TOOLS_BY_SOURCE_ID) | {"ncbi"}
    for source in SOURCE_IDS:
        profile = load_profile(source)
        assert profile.source_id == source and profile.scope == f"cei-tools/src.{source}"
    with pytest.raises(ValueError):
        load_profile("../pubmed")
    with pytest.raises(SourceUnavailableError):
        source_tool(load_profile("ncbi"))


@pytest.mark.asyncio
@pytest.mark.parametrize("source", sorted(TOOLS_BY_SOURCE_ID))
async def test_graph_only_invokes_selected_source(source):
    tool = FakeTool(source)
    model = FakeModel(source)
    graph = compile_graph(load_profile(source), InMemorySaver(), model=model, tool=tool)
    result = await graph.ainvoke({"messages": [HumanMessage(content="question")], "tool_rounds": 0},
                                 {"configurable": {"thread_id": "session"}})
    assert tool.calls == [{"query": "x"}]
    assert result["messages"][-1].content == "Found a record."
    assert "rejected" in result["messages"][-3].content


def test_ncbi_tools_require_gateway_and_call_only_named_targets(monkeypatch):
    from tool_agent import ncbi_tools as module
    from shared.tools.gateway_mcp import GatewayCallError
    tools = {tool.name: tool for tool in ncbi_tools()}
    assert set(tools) == {"ncbi_taxonomy_lineage", "ncbi_sequence_efetch"}
    monkeypatch.setattr(module, "current_gateway_token", lambda: None)
    with pytest.raises(GatewayCallError):
        tools["ncbi_taxonomy_lineage"].invoke({"taxon": "human"})
    calls = []
    monkeypatch.setenv("CEI_TOOL_GATEWAY_URL", "https://example.invalid/mcp")
    monkeypatch.setattr(module, "current_gateway_token", lambda: "token")
    monkeypatch.setattr(module, "call_gateway_tool", lambda url, token, target, args, **kw: calls.append((target, kw["tool_name"], args)) or {"result": {}})
    tools["ncbi_taxonomy_lineage"].invoke({"taxon": "human"})
    tools["ncbi_sequence_efetch"].invoke({"accession": "ABC123", "database": "protein", "format": "fasta"})
    assert calls == [("ncbi-taxonomy", "lineage", {"taxon": "human"}),
                     ("ncbi-sequence", "efetch", {"accession": "ABC123", "database": "protein", "format": "fasta"})]


@pytest.mark.asyncio
async def test_ncbi_graph_rejects_other_source_tools(monkeypatch):
    from tool_agent import ncbi_tools as module
    taxonomy, sequence = FakeTool("taxonomy"), FakeTool("sequence")
    monkeypatch.setattr(module, "ncbi_tools", lambda: [taxonomy, sequence])

    class NcbiModel(FakeModel):
        def bind_tools(self, tools):
            assert {item.name for item in tools} == {"taxonomy", "sequence"}
            return self

    graph = compile_graph(load_profile("ncbi"), InMemorySaver(), model=NcbiModel("taxonomy"))
    result = await graph.ainvoke({"messages": [HumanMessage(content="question")], "tool_rounds": 0},
                                 {"configurable": {"thread_id": "ncbi-session"}})
    assert taxonomy.calls == [{"query": "x"}] and sequence.calls == []
    assert "rejected" in result["messages"][-3].content


def test_grant_must_match_user_session_and_exact_source():
    profile = load_profile("pubmed")
    class Verifier:
        claims = {"scope": "cei-tools/common cei-tools/src.pubmed", "cei_user_sub": "u", "cei_session_id": "s"}
        def verify(self, token):
            return self.claims
    verifier = Verifier()
    authorize_grant("token", verifier, profile, "u", "s")
    with pytest.raises(UnauthenticatedError):
        authorize_grant("token", verifier, profile, "other", "s")
    verifier.claims = {**verifier.claims, "scope": "cei-tools/common cei-tools/src.pubmed cei-tools/src.ncbi"}
    with pytest.raises(UnauthenticatedError):
        authorize_grant("token", verifier, profile, "u", "s")
    assert source_thread("s", "pubmed") != source_thread("s", "ncbi")
