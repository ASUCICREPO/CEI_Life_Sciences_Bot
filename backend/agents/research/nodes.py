from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage

from research.state import ResearchState, TriagePlan
from shared.grounding import build_corpus, ground_document
from shared.presenters.markdown import render_document_markdown
from shared.prompts.loader import PromptLoader
from shared.schemas.citation import SourceType
from shared.schemas.response_blocks import ResponseDocument
from shared.state import TraceEntry
from shared.structured import invoke_structured
from shared.tools.data_sources import ALL_DATA_SOURCE_TOOLS
from shared.tools.base import GatewayUnavailableError
from shared.utils import extract_tokens, get_model, last_human_message

logger = logging.getLogger(__name__)

_RESEARCH_TEMPLATES_DIR = Path(__file__).parent / "prompts" / "templates"

_SOURCE_DESCRIPTIONS: dict[SourceType, str] = {
    SourceType.PUBMED: "PubMed — peer-reviewed biomedical literature, abstracts, citations",
    SourceType.CLINICAL_TRIALS: "ClinicalTrials.gov — registered clinical studies, sponsors, status",
    SourceType.BIORXIV: "bioRxiv — biology preprints (not yet peer-reviewed)",
    SourceType.MEDRXIV: "medRxiv — medical/clinical preprints (not yet peer-reviewed)",
    SourceType.CHEMBL: "ChEMBL — bioactive molecules, drug compounds, biological targets",
    SourceType.OPEN_TARGETS: "Open Targets — gene-disease-drug associations and evidence scores",
    SourceType.CMS_COVERAGE: "CMS Coverage Database — US Medicare/Medicaid coverage policies",
    SourceType.ICD10: "ICD-10 — disease and condition classification codes",
    SourceType.NPI_REGISTRY: "NPI Registry — US healthcare provider directory",
    SourceType.PUBCHEM: "PubChem — chemical structures, properties, bioactivity data",
    SourceType.OPENFDA: "openFDA — drug approvals, drug labels, device 510(k) clearances",
    SourceType.PATENTSVIEW: "USPTO PatentsView — patent search by assignee, keyword, and CPC",
}

_TRIAGE_SYSTEM = """\
You are a research triage assistant for a life sciences bot. Given a user query, decide which data sources to search and what optimized search query to send to each.

RULES:
1. Only select sources that are genuinely relevant. If the query is outside the scope of ALL available sources, return an empty selected_sources list.
2. For each selected source, write an optimized search query tailored to that source's API. Use precise scientific terms, database-specific keywords, and remove conversational filler.
3. Different sources need different query styles:
   - PubMed: use MeSH-style terms and Boolean operators when helpful
   - ClinicalTrials.gov: use condition/intervention terms
   - ChEMBL/PubChem: use molecule names, targets, or compound identifiers
   - Open Targets: use gene symbols or disease names
   - ICD-10: use disease names or codes
   - NPI Registry: use provider names or specialties
4. Presentation requests do not determine source selection. Ignore requests to use a UI protocol, show UI elements, create interactive graphics or charts, or apply output formatting. Select sources using only the substantive life-sciences research question.

Respond ONLY with valid JSON matching this schema:
{
  "selected_sources": ["source_key", ...],
  "source_queries": {"source_key": "optimized query for this source", ...},
  "reasoning": "brief explanation of source selection and query strategy"
}

Valid source keys: """ + ", ".join(s.value for s in SourceType)

_PRESENTATION_ONLY_PHRASES = re.compile(
    r"\b(?:use (?:the )?ui protocol|show ui elements|interactive graphics?|charts?|output formatting)\b",
    re.IGNORECASE,
)

_SYNTHESIS_PRESENTATION_RULE = """\
Answer the scientific question in text. Never claim that you can or cannot generate UI or interactive graphics, and never output CEI UI JSON. The application handles rendering when needed; focus on the research answer.
"""

_SYNTHESIS_MAX_TOKENS = 8192


def estimate_research_duration_seconds(plan: TriagePlan) -> int:
    """Conservative source-budget estimate, derived only from the server's triage.

    Retrieval currently runs serially. Gateway source Lambdas have 90-second
    timeouts except openFDA (150); allow another 60 seconds for model stages.
    It is an estimate, not a guarantee about model or upstream latency.
    """
    sources = set(plan.selected_sources) or {SourceType.PUBMED}
    return 60 + sum(150 if source == SourceType.OPENFDA else 90 for source in sources)


def _source_query_args(source: SourceType, query: str) -> dict[str, str]:
    # These source APIs use distinct names for their primary search field.
    if source == SourceType.OPEN_TARGETS:
        return {"target": query}
    if source == SourceType.NPI_REGISTRY:
        return {"name": query}
    return {"query": query}


def _fallback_pubmed_query(query: str) -> str:
    cleaned_query = _PRESENTATION_ONLY_PHRASES.sub("", query)
    cleaned_query = re.sub(r"\b(?:and|or)\b\s*(?=$|[.,;:])", "", cleaned_query, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", cleaned_query).strip(" ,.;:-")


def triage(state: ResearchState) -> dict[str, Any]:
    query = last_human_message(state)
    source_list = "\n".join(f"- {k.value}: {v}" for k, v in _SOURCE_DESCRIPTIONS.items())
    user_prompt = f"Available sources:\n{source_list}\n\nUser query: {query}"

    model = get_model().with_structured_output(TriagePlan, include_raw=True)
    raw_result = model.invoke([
        {"role": "system", "content": _TRIAGE_SYSTEM},
        {"role": "user", "content": user_prompt},
    ])
    plan: TriagePlan = raw_result["parsed"]
    in_tok, out_tok = extract_tokens(raw_result["raw"])

    sources_str = ", ".join(s.value for s in plan.selected_sources) or "(none)"
    queries_str = "; ".join(f"{k}={v!r}" for k, v in plan.source_queries.items())
    trace = TraceEntry(
        stage="TRIAGE",
        detail=f"Sources: {sources_str} | Queries: {queries_str} | Reasoning: {plan.reasoning}",
        input_tokens=in_tok,
        output_tokens=out_tok,
    )

    logger.info({"event": "triage_complete", "sources": [s.value for s in plan.selected_sources]})
    return {"query": query, "triage_plan": plan, "trace": [trace]}


def retrieve(state: ResearchState) -> dict[str, Any]:
    plan = state.get("triage_plan")
    tool_map = {t.source_type: t for t in ALL_DATA_SOURCE_TOOLS}
    if not plan or not plan.selected_sources:
        query = state.get("query", "")
        pubmed = tool_map.get(SourceType.PUBMED)
        if not query or pubmed is None:
            trace = TraceEntry(stage="RETRIEVE", detail="Skipped — no sources selected")
            return {"retrieved_results": [], "trace": [trace]}

        fallback_query = _fallback_pubmed_query(query)
        try:
            result = pubmed.invoke(query=fallback_query)
            trace = TraceEntry(
                stage="TOOL_CALL",
                detail=(
                    "Fallback behavior: PubMed invoked after triage selected no sources "
                    f"| query={fallback_query!r} | results={len(result.results)} "
                    f"| citations={len(result.citations)}"
                ),
            )
            logger.info({"event": "retrieve_pubmed_fallback", "count": len(result.results)})
            return {"retrieved_results": [result], "trace": [trace]}
        except GatewayUnavailableError:
            raise
        except Exception:
            logger.error({"event": "retrieve_pubmed_fallback_error"})
            trace = TraceEntry(
                stage="TOOL_CALL",
                detail=f"Fallback behavior: PubMed invoked after triage selected no sources | query={fallback_query!r} | ERROR",
            )
            return {"retrieved_results": [], "trace": [trace]}

    results = []
    trace_entries: list[TraceEntry] = []

    for source in plan.selected_sources:
        tool = tool_map.get(source)
        if tool is None:
            logger.warning({"event": "tool_not_found", "source": source})
            continue
        search_query = plan.source_queries.get(source.value, state["query"])
        try:
            result = tool.invoke(**_source_query_args(source, search_query))
            results.append(result)
            trace_entries.append(TraceEntry(
                stage="TOOL_CALL",
                detail=f"{source.value} | query={search_query!r} | results={len(result.results)} | citations={len(result.citations)}",
            ))
            logger.info({"event": "retrieve_success", "source": source, "count": len(result.results)})
        except GatewayUnavailableError:
            raise
        except Exception as exc:
            logger.error({"event": "retrieve_error", "source": source, "error_type": type(exc).__name__})
            trace_entries.append(TraceEntry(
                stage="TOOL_CALL",
                detail=f"{source.value} | query={search_query!r} | ERROR",
            ))

    return {"retrieved_results": results, "trace": trace_entries}


def synthesize(state: ResearchState) -> dict[str, Any]:
    loader = PromptLoader(agent_templates_dir=_RESEARCH_TEMPLATES_DIR)
    mode = state.get("mode", "science")
    query = state["query"]

    context_parts: list[str] = []
    evidence_texts: list[str] = []
    for res in state.get("retrieved_results", []):
        context_parts.append(f"### {res.source_type.value.upper()} RESULTS")
        for item in res.results:
            context_parts.append(str(item))
            evidence_texts.append(str(item))
        for cit in res.citations:
            context_parts.append(
                f"[{cit.source_id}] {cit.title} ({cit.year}) — {cit.url}"
            )
            evidence_texts.append(cit.title)
            if cit.relevant_passage:
                evidence_texts.append(cit.relevant_passage)

    context = "\n".join(context_parts)
    user_prompt = (
        f"Query: {query}\n\n"
        f"Evidence:\n{context}\n\n"
        "Synthesize a comprehensive answer with inline citations using [SourceID] notation."
    ) if context else f"Query: {query}"

    structured_user_prompt = (
        f"Query: {query}\n\n"
        f"Evidence:\n{context}\n\n"
        "Compose the answer as a visual report with inline [SourceID] citations. "
        "Lead with graphics and structured blocks; use prose only where explanation is required."
    ) if context else f"Query: {query}"
    structured_system_prompt = loader.system_prompt(mode, structured_blocks=True)
    on_prose_delta = None
    if state.get("stream"):
        from langgraph.config import get_stream_writer
        writer = get_stream_writer()
        on_prose_delta = lambda delta: writer({"type": "prose_delta", "delta": delta})
    parsed, raw, error = invoke_structured(
        get_model(max_tokens=_SYNTHESIS_MAX_TOKENS),
        ResponseDocument,
        [
            {"role": "system", "content": structured_system_prompt},
            {"role": "user", "content": structured_user_prompt},
        ],
        **({"on_prose_delta": on_prose_delta} if on_prose_delta is not None else {}),
    )

    trace_entries: list[TraceEntry] = []
    if parsed is not None:
        try:
            if state.get("stream"):
                in_tok, out_tok = extract_tokens(raw) if raw is not None else (0, 0)
                return {"synthesis": render_document_markdown(parsed), "response_document": parsed,
                        "grounding_pending": True,
                        "trace": [TraceEntry(stage="SYNTHESIS", detail=f"mode={mode} | structured=true",
                                             input_tokens=in_tok, output_tokens=out_tok)]}
            corpus = build_corpus(query, *context_parts, *evidence_texts)
            doc, notes = ground_document(parsed, corpus)
            synthesis_text = render_document_markdown(doc)
            in_tok, out_tok = extract_tokens(raw) if raw is not None else (0, 0)
            trace_entries.append(TraceEntry(
                stage="SYNTHESIS",
                detail=f"mode={mode} | structured=true | response_length={len(synthesis_text)}",
                input_tokens=in_tok,
                output_tokens=out_tok,
            ))
            trace_entries.extend(TraceEntry(stage="GROUNDING", detail=note) for note in notes)
            logger.info({"event": "synthesize_complete", "length": len(synthesis_text), "structured": True})
            return {
                "synthesis": synthesis_text,
                "response_document": doc,
                "messages": [AIMessage(content=synthesis_text)],
                "trace": trace_entries,
            }
        except Exception as exc:  # ADR: any render/grounding failure falls back to prose | never lose the answer
            logger.error({"event": "synthesize_structured_render_error", "error_type": type(exc).__name__})
            error = type(exc).__name__

    trace_entries.append(TraceEntry(
        stage="SYNTHESIS_FALLBACK",
        detail=f"mode={mode} | error={error}",
    ))

    # Existing prose path, unchanged.
    system_prompt = loader.system_prompt(mode) + "\n" + _SYNTHESIS_PRESENTATION_RULE
    model = get_model()
    response = model.invoke([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ])
    synthesis_text = str(response.content)
    in_tok, out_tok = extract_tokens(response)

    trace_entries.append(TraceEntry(
        stage="SYNTHESIS",
        detail=f"mode={mode} | response_length={len(synthesis_text)}",
        input_tokens=in_tok,
        output_tokens=out_tok,
    ))

    logger.info({"event": "synthesize_complete", "length": len(synthesis_text)})
    return {
        "synthesis": synthesis_text,
        "response_document": None,
        "messages": [AIMessage(content=synthesis_text)],
        "trace": trace_entries,
    }


def ground_synthesis(state: ResearchState) -> dict[str, Any]:
    """Ground the structured streaming draft after its pending blocks are emitted."""
    if not state.get("grounding_pending"):
        return {}
    context_parts: list[str] = []
    for result in state.get("retrieved_results", []):
        context_parts.extend(str(item) for item in result.results)
        for citation in result.citations:
            context_parts.extend([citation.title, citation.relevant_passage or ""])
    corpus = build_corpus(state["query"], *context_parts)
    doc, notes = ground_document(state["response_document"], corpus)
    synthesis_text = render_document_markdown(doc)
    return {"synthesis": synthesis_text, "response_document": doc, "grounding_pending": False,
            "messages": [AIMessage(content=synthesis_text)],
            "trace": [TraceEntry(stage="GROUNDING", detail=note) for note in notes]}


def cite(state: ResearchState) -> dict[str, Any]:
    synthesis = state.get("synthesis", "")
    all_citations = {
        cit.source_id: cit
        for res in state.get("retrieved_results", [])
        for cit in res.citations
    }

    referenced = {
        source_id.strip()
        for group in re.findall(r"\[([^\]]+)\]", synthesis)
        for source_id in re.split(r"[,;]", group)
        if source_id.strip()
    }
    bibliography_lines: list[str] = []
    for sid in sorted(referenced):
        cit = all_citations.get(sid)
        if cit:
            authors = ", ".join(cit.authors[:3]) + (" et al." if len(cit.authors) > 3 else "")
            bibliography_lines.append(
                f"[{sid}] {authors} ({cit.year}). {cit.title}. {cit.url}"
            )
        else:
            logger.warning({"event": "unresolved_citation", "source_id": sid})

    if bibliography_lines:
        final_response = synthesis + "\n\n---\n**References**\n" + "\n".join(bibliography_lines)
    else:
        final_response = synthesis

    trace = TraceEntry(
        stage="CITE",
        detail=f"resolved={len(bibliography_lines)} | referenced={len(referenced)} | unresolved={len(referenced) - len(bibliography_lines)}",
    )

    logger.info({"event": "cite_complete", "resolved": len(bibliography_lines), "referenced": len(referenced)})
    return {"messages": [AIMessage(content=final_response)], "trace": [trace]}
