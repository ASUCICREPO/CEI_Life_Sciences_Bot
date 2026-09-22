from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from langchain_aws import ChatBedrockConverse
from langchain_core.messages import AIMessage, HumanMessage

from research.state import ResearchState, TraceEntry, TriagePlan
from shared.config import config
from shared.prompts.loader import PromptLoader
from shared.schemas.citation import SourceType
from shared.tools.data_sources import ALL_DATA_SOURCE_TOOLS

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

Respond ONLY with valid JSON matching this schema:
{
  "selected_sources": ["source_key", ...],
  "source_queries": {"source_key": "optimized query for this source", ...},
  "reasoning": "brief explanation of source selection and query strategy"
}

Valid source keys: """ + ", ".join(s.value for s in SourceType)


def _get_model(model_id: str | None = None) -> ChatBedrockConverse:
    return ChatBedrockConverse(
        model_id=model_id or config.default_model_id,
        region_name=config.aws_region,
        credentials_profile_name=config.aws_profile,
    )


def _extract_tokens(response: Any) -> tuple[int, int]:
    usage = getattr(response, "usage_metadata", None)
    if usage:
        return usage.get("input_tokens", 0), usage.get("output_tokens", 0)
    return 0, 0


def _last_human_message(state: ResearchState) -> str:
    for msg in reversed(state["messages"]):
        if isinstance(msg, HumanMessage):
            return str(msg.content)
    return ""


def triage(state: ResearchState) -> dict[str, Any]:
    query = _last_human_message(state)
    source_list = "\n".join(f"- {k.value}: {v}" for k, v in _SOURCE_DESCRIPTIONS.items())
    user_prompt = f"Available sources:\n{source_list}\n\nUser query: {query}"

    model = _get_model().with_structured_output(TriagePlan, include_raw=True)
    raw_result = model.invoke([
        {"role": "system", "content": _TRIAGE_SYSTEM},
        {"role": "user", "content": user_prompt},
    ])
    plan: TriagePlan = raw_result["parsed"]
    in_tok, out_tok = _extract_tokens(raw_result["raw"])

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
    if not plan or not plan.selected_sources:
        trace = TraceEntry(stage="RETRIEVE", detail="Skipped — no sources selected")
        return {"retrieved_results": [], "trace": [trace]}

    tool_map = {t.source_type: t for t in ALL_DATA_SOURCE_TOOLS}
    results = []
    trace_entries: list[TraceEntry] = []

    for source in plan.selected_sources:
        tool = tool_map.get(source)
        if tool is None:
            logger.warning({"event": "tool_not_found", "source": source})
            continue
        search_query = plan.source_queries.get(source.value, state["query"])
        try:
            result = tool.invoke(query=search_query)
            results.append(result)
            trace_entries.append(TraceEntry(
                stage="TOOL_CALL",
                detail=f"{source.value} | query={search_query!r} | results={len(result.results)} | citations={len(result.citations)}",
            ))
            logger.info({"event": "retrieve_success", "source": source, "query": search_query, "count": len(result.results)})
        except Exception:
            logger.exception({"event": "retrieve_error", "source": source})
            trace_entries.append(TraceEntry(
                stage="TOOL_CALL",
                detail=f"{source.value} | query={search_query!r} | ERROR",
            ))

    return {"retrieved_results": results, "trace": trace_entries}


def synthesize(state: ResearchState) -> dict[str, Any]:
    loader = PromptLoader(agent_templates_dir=_RESEARCH_TEMPLATES_DIR)
    mode = state.get("mode", "science")
    system_prompt = loader.system_prompt(mode)

    context_parts: list[str] = []
    for res in state.get("retrieved_results", []):
        context_parts.append(f"### {res.source_type.value.upper()} RESULTS")
        for item in res.results:
            context_parts.append(str(item))
        for cit in res.citations:
            context_parts.append(
                f"[{cit.source_id}] {cit.title} ({cit.year}) — {cit.url}"
            )

    context = "\n".join(context_parts)
    user_prompt = (
        f"Query: {state['query']}\n\n"
        f"Evidence:\n{context}\n\n"
        "Synthesize a comprehensive answer with inline citations using [SourceID] notation."
    ) if context else f"Query: {state['query']}"

    model = _get_model()
    response = model.invoke([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ])
    synthesis_text = str(response.content)
    in_tok, out_tok = _extract_tokens(response)

    trace = TraceEntry(
        stage="SYNTHESIS",
        detail=f"mode={mode} | response_length={len(synthesis_text)}",
        input_tokens=in_tok,
        output_tokens=out_tok,
    )

    logger.info({"event": "synthesize_complete", "length": len(synthesis_text)})
    return {
        "synthesis": synthesis_text,
        "messages": [AIMessage(content=synthesis_text)],
        "trace": [trace],
    }


def cite(state: ResearchState) -> dict[str, Any]:
    synthesis = state.get("synthesis", "")
    all_citations = {
        cit.source_id: cit
        for res in state.get("retrieved_results", [])
        for cit in res.citations
    }

    referenced = set(re.findall(r"\[([^\]]+)\]", synthesis))
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
