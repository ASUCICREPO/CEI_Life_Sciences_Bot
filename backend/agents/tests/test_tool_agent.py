from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from shared.runtime.identity import UnauthenticatedError
from shared.tools.data_sources import TOOLS_BY_SOURCE_ID
from tool_agent.graph import SourceUnavailableError, compile_graph, source_tool
from tool_agent.main import authorize_grant, source_thread
from tool_agent.profile import SOURCE_IDS, load_profile


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
