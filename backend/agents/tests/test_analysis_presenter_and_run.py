from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import Mock

from langchain_core.messages import AIMessage, HumanMessage

import analysis_main
from analysis.run import run_analysis
from shared.presenters import build_analysis_surface
from shared.schemas.citation import Citation, SourceType
from shared.schemas.response_blocks import ProseBlock, ResponseDocument, ResponseSection
from shared.tools.base import DataSourceResult


def _citation() -> Citation:
    return Citation(
        source_id="PMID:1",
        source_type=SourceType.PUBMED,
        url="https://pubmed.ncbi.nlm.nih.gov/1/",
        title="Evidence",
        authors=["Researcher"],
        year=2026,
        relevant_passage="Evidence passage.",
        retrieved_at=datetime.now(UTC),
    )


def _retrieved_results() -> list[DataSourceResult]:
    return [DataSourceResult(source_type=SourceType.PUBMED, results=[{"title": "Record"}], citations=[_citation()])]


def test_build_analysis_surface_legacy_text_is_valid() -> None:
    surface = build_analysis_surface(
        "# Report\n\nFindings here.",
        surface_id="analysis-final",
        retrieved_results=_retrieved_results(),
        document=None,
    )
    assert surface.surface_id == "analysis-final"
    assert surface.version == "1.1"


def test_build_analysis_surface_document_is_valid() -> None:
    document = ResponseDocument(
        title="Analysis",
        summary="Summary",
        key_metrics=[],
        sections=[ResponseSection(heading="Findings", blocks=[ProseBlock(kind="prose", markdown="Finding. [PMID:1]")])],
    )
    surface = build_analysis_surface(
        "ignored",
        surface_id="analysis-final",
        retrieved_results=_retrieved_results(),
        document=document,
    )
    assert surface.surface_id == "analysis-final"
    assert len(surface.components) <= 250


# ── run_analysis (Pipeline Manager contract) ────────────────────────────────


def test_run_analysis_skips_interrupt_and_returns_outcome(monkeypatch) -> None:
    raw = AIMessage(content="")
    structured_model = Mock()
    structured_model.invoke.return_value = {"parsed": None, "raw": raw, "parsing_error": "n/a"}
    model = Mock()
    model.with_structured_output.return_value = structured_model
    model.invoke.return_value = AIMessage(content="Final report text.")
    monkeypatch.setattr("analysis.nodes.get_model", lambda *a, **k: model)
    monkeypatch.setattr("analysis.nodes.ALL_DATA_SOURCE_TOOLS", [])

    outcome = asyncio.run(run_analysis(
        "Analyze Our Co vs Acme Bio",
        "science",
        "pipeline-session-1",
        analysis_type="competitor",
        scope={"competitors": ["Acme Bio"], "focal_entity": "Our Co"},
    ))

    assert outcome.text == "Final report text."
    assert outcome.document is None
    assert isinstance(outcome.retrieved_results, list)


# ── analysis_main runtime op contract ───────────────────────────────────────


class _Context:
    session_id = "runtime-session"


class _CompletedGraph:
    def __init__(self, result: dict) -> None:
        self._result = result

    async def ainvoke(self, state, config):
        return self._result


def test_analysis_main_run_returns_awaiting_input_on_interrupt(monkeypatch) -> None:
    class _Interrupt:
        def __init__(self, value):
            self.value = value

    surface = {
        "content_type": "application/vnd.cei.ui+json",
        "protocol": "cei-ui",
        "version": "1.1",
        "surface_id": "analysis-scope",
        "revision": 1,
        "root_id": "root",
        "components": [{"id": "root", "type": "Text", "content": "x", "variant": "body"}],
    }
    result = {"__interrupt__": [_Interrupt(surface)]}

    import analysis.graph as graph_module
    monkeypatch.setattr(graph_module, "compile_graph", lambda **_: _CompletedGraph(result))

    response = asyncio.run(analysis_main.invoke({"prompt": "Analyze Our Co"}, _Context()))
    assert response["status"] == "awaiting_input"
    assert response["ui"]["surface_id"] == "analysis-scope"


def test_analysis_main_resume_stale_without_pending(monkeypatch) -> None:
    payload = {
        "op": "resume",
        "action": {
            "request_id": "r1",
            "session_id": "runtime-session-2",
            "surface_id": "analysis-scope",
            "revision": 1,
            "interaction_id": "analysis-scope-review",
            "component_id": "scope-review",
            "action_id": "approve-scope",
            "value": None,
        },
    }

    class _Context2:
        session_id = "runtime-session-2"

    response = asyncio.run(analysis_main.invoke(payload, _Context2()))
    assert response["status"] == "stale"


def test_analysis_main_resume_completes_run(monkeypatch) -> None:
    result = {
        "messages": [
            HumanMessage(content="Analyze Our Co"),
            AIMessage(content="Final report."),
        ],
        "trace": [],
        "retrieved_results": [],
        "response_document": None,
    }

    import analysis.graph as graph_module
    monkeypatch.setattr(graph_module, "compile_graph", lambda **_: _CompletedGraph(result))

    from shared.tools.hitl.inline_functions import build_scope_confirmation_surface
    surface = build_scope_confirmation_surface(
        surface_id="analysis-scope", revision=1, analysis_type="competitor",
        competitors=[], market=None, jurisdiction=None, focal_entity=None,
    )
    analysis_main._pending.register("runtime-session-3", surface)

    class _Context3:
        session_id = "runtime-session-3"

    payload = {
        "op": "resume",
        "action": {
            "request_id": "r1",
            "session_id": "runtime-session-3",
            "surface_id": "analysis-scope",
            "revision": 1,
            "interaction_id": "analysis-scope-review",
            "component_id": "scope-review",
            "action_id": "approve-scope",
            "value": None,
        },
    }
    response = asyncio.run(analysis_main.invoke(payload, _Context3()))
    assert response["status"] == "completed"
    assert response["text"] == "Final report."
    assert response["action_response"]["status"] == "completed"
