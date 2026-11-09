from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage
from langgraph.types import interrupt

from analysis.state import (
    AnalysisState,
    AnalysisType,
    Comparison,
    ComparisonsOutput,
    GatherQueries,
    ScopeProposal,
)
from shared.grounding import build_corpus, ground_document
from shared.presenters.markdown import render_document_markdown
from shared.prompts.loader import PromptLoader
from shared.schemas.citation import SourceType
from shared.schemas.response_blocks import (
    ComparisonBlock,
    ComparisonCriterionItem,
    ResponseDocument,
    TableBlock,
)
from shared.state import TraceEntry
from shared.structured import invoke_structured
from shared.tools.data_sources import ALL_DATA_SOURCE_TOOLS
from shared.tools.hitl.inline_functions import build_scope_confirmation_surface
from shared.utils import extract_tokens, get_model, last_human_message

logger = logging.getLogger(__name__)

_ANALYSIS_TEMPLATES_DIR = Path(__file__).parent / "prompts" / "templates"

_SYNTHESIS_MAX_TOKENS = 8192

# ADR: fixed per-type source map (B94) | Pipeline Manager MVP needs deterministic gather, not a triage call
_SOURCE_MAP: dict[AnalysisType, list[SourceType]] = {
    AnalysisType.COMPETITOR: [
        SourceType.CLINICAL_TRIALS,
        SourceType.OPEN_TARGETS,
        SourceType.CHEMBL,
        SourceType.PUBMED,
        SourceType.OPENFDA,
        SourceType.PATENTSVIEW,
    ],
    AnalysisType.MARKET: [
        SourceType.CLINICAL_TRIALS,
        SourceType.CMS_COVERAGE,
        SourceType.ICD10,
        SourceType.OPEN_TARGETS,
        SourceType.PUBMED,
        SourceType.OPENFDA,
    ],
    AnalysisType.LEGAL_LANDSCAPE: [
        SourceType.OPENFDA,
        SourceType.PATENTSVIEW,
        SourceType.CMS_COVERAGE,
        SourceType.CLINICAL_TRIALS,
        SourceType.PUBMED,
    ],
}

_COMPARE_TEMPLATE: dict[AnalysisType, str] = {
    AnalysisType.COMPETITOR: "compare_competitor.j2",
    AnalysisType.MARKET: "compare_market.j2",
    AnalysisType.LEGAL_LANDSCAPE: "compare_legal_landscape.j2",
}

_FIXED_DIMENSIONS: dict[AnalysisType, list[str]] = {
    AnalysisType.COMPETITOR: [
        "Modality / mechanism",
        "Lead indication",
        "Development stage",
        "Trial activity",
        "Approvals",
        "Patent position",
        "Differentiation",
    ],
    AnalysisType.MARKET: [
        "Patient population",
        "Standard of care",
        "Reimbursement / coverage",
        "Pipeline density",
        "Key players",
        "Drivers and barriers",
    ],
    AnalysisType.LEGAL_LANDSCAPE: [
        "Regulatory pathway and designations",
        "Approvals and labels",
        "Patent landscape and expiries",
        "Coverage and reimbursement",
        "Open risks",
    ],
}


def _dedupe(names: list[str]) -> list[str]:
    """Keep the first spelling of each name; the model sometimes repeats a product."""
    seen: set[str] = set()
    unique: list[str] = []
    for name in names:
        key = name.split("(")[0].strip().casefold()
        if key and key not in seen:
            seen.add(key)
            unique.append(name.strip())
    return unique


def scope(state: AnalysisState) -> dict[str, Any]:
    """Extract (or re-propose, after a user correction) the analysis scope."""
    query = state.get("query") or last_human_message(state)
    correction = state.get("scope_correction")
    forced_type = state.get("analysis_type")

    loader = PromptLoader(agent_templates_dir=_ANALYSIS_TEMPLATES_DIR)
    system_prompt = loader.render("scope.j2", correction=correction)

    parsed, raw, error = invoke_structured(
        get_model(),
        ScopeProposal,
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"User request: {query}"},
        ],
    )
    in_tok, out_tok = extract_tokens(raw) if raw is not None else (0, 0)
    revision = state.get("scope_revision", 0) + 1

    if parsed is not None:
        analysis_type = forced_type if forced_type is not None else parsed.analysis_type
        competitors = _dedupe(parsed.competitors)
        market = parsed.market
        jurisdiction = parsed.jurisdiction
        focal_entity = parsed.focal_entity
        detail = (
            f"type={analysis_type.value} | competitors={competitors} | "
            f"market={market} | jurisdiction={jurisdiction} | revision={revision}"
        )
    else:
        # ADR: never crash on a parse failure | fall back to the caller's own scope (B94)
        analysis_type = forced_type if forced_type is not None else AnalysisType.COMPETITOR
        competitors = state.get("competitors", [])
        market = state.get("market")
        jurisdiction = state.get("jurisdiction")
        focal_entity = state.get("focal_entity")
        detail = f"fallback | revision={revision} | error={error}"

    trace = TraceEntry(stage="SCOPE", detail=detail, input_tokens=in_tok, output_tokens=out_tok)
    logger.info({"event": "scope_complete", "analysis_type": analysis_type.value, "revision": revision})
    return {
        "query": query,
        "analysis_type": analysis_type,
        "competitors": competitors,
        "market": market,
        "jurisdiction": jurisdiction,
        "focal_entity": focal_entity,
        "scope_revision": revision,
        "scope_correction": None,
        "trace": [trace],
    }


def confirm_scope(state: AnalysisState) -> dict[str, Any]:
    """Pause for scope confirmation unless the caller (for example the Pipeline Manager) already confirmed it."""
    if state.get("confirmed_scope"):
        return {}

    analysis_type: AnalysisType = state["analysis_type"]
    surface = build_scope_confirmation_surface(
        surface_id="analysis-scope",
        revision=state.get("scope_revision", 1),
        analysis_type=analysis_type.value,
        competitors=state.get("competitors", []),
        market=state.get("market"),
        jurisdiction=state.get("jurisdiction"),
        focal_entity=state.get("focal_entity"),
    )
    decision = interrupt(surface)
    action_id = decision.get("action_id") if isinstance(decision, dict) else None

    if action_id == "approve-scope":
        logger.info({"event": "scope_confirmed"})
        return {"confirmed_scope": True, "trace": [TraceEntry(stage="SCOPE_CONFIRM", detail="approved")]}

    if action_id == "reject-scope":
        logger.info({"event": "scope_rejected"})
        return {
            "confirmed_scope": False,
            "scope_correction": None,
            "messages": [AIMessage(content="Analysis cancelled at the user's request.")],
            "trace": [TraceEntry(stage="SCOPE_CONFIRM", detail="rejected")],
        }

    if action_id == "edit-scope":
        value = decision.get("value") if isinstance(decision, dict) else None
        correction = value if isinstance(value, str) else str(value)
        logger.info({"event": "scope_edited"})
        return {
            "confirmed_scope": False,
            "scope_correction": correction,
            "trace": [TraceEntry(stage="SCOPE_CONFIRM", detail=f"edited: {correction}")],
        }

    logger.warning({"event": "scope_confirm_unknown_action", "action_id": action_id})
    return {
        "confirmed_scope": False,
        "scope_correction": None,
        "messages": [AIMessage(content="Analysis cancelled — the scope confirmation was not understood.")],
        "trace": [TraceEntry(stage="SCOPE_CONFIRM", detail=f"unknown action: {action_id}")],
    }


def gather(state: AnalysisState) -> dict[str, Any]:
    """Collect raw data from the fixed source map for the confirmed analysis type."""
    analysis_type: AnalysisType = state["analysis_type"]
    sources = _SOURCE_MAP[analysis_type]
    query = state.get("query", "")

    loader = PromptLoader(agent_templates_dir=_ANALYSIS_TEMPLATES_DIR)
    system_prompt = loader.render(
        "gather.j2",
        analysis_type=analysis_type.value,
        competitors=state.get("competitors", []),
        market=state.get("market"),
        jurisdiction=state.get("jurisdiction"),
        focal_entity=state.get("focal_entity"),
        sources=[s.value for s in sources],
    )
    parsed: GatherQueries | None = None
    try:
        model = get_model().with_structured_output(GatherQueries, include_raw=True)
        raw_result = model.invoke([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"User request: {query}"},
        ])
        parsed = raw_result.get("parsed")
    except Exception as exc:
        logger.error({"event": "gather_query_planning_error", "error_type": type(exc).__name__})

    tool_map = {t.source_type: t for t in ALL_DATA_SOURCE_TOOLS}
    results = []
    trace_entries: list[TraceEntry] = []

    for source in sources:
        tool = tool_map.get(source)
        if tool is None:
            logger.warning({"event": "tool_not_found", "source": source})
            continue
        search_query = (parsed.source_queries.get(source.value) if parsed else None) or query
        try:
            result = tool.invoke(query=search_query)
            results.append(result)
            trace_entries.append(TraceEntry(
                stage="TOOL_CALL",
                detail=f"{source.value} | query={search_query!r} | results={len(result.results)} | citations={len(result.citations)}",
            ))
            logger.info({"event": "gather_success", "source": source, "count": len(result.results)})
        except Exception as exc:
            logger.error({"event": "gather_error", "source": source, "error_type": type(exc).__name__})
            trace_entries.append(TraceEntry(
                stage="TOOL_CALL",
                detail=f"{source.value} | query={search_query!r} | ERROR",
            ))

    return {"retrieved_results": results, "trace": trace_entries}


def analyze(state: AnalysisState) -> dict[str, Any]:
    """Turn gathered DataSourceResults into short structured findings, one per source."""
    findings: list[str] = []
    trace_entries: list[TraceEntry] = []

    for result in state.get("retrieved_results", []):
        source_label = result.source_type.value
        citation_ids = ", ".join(c.source_id for c in result.citations[:5]) or "(no citations)"
        item_count = len(result.results)
        finding = f"{source_label}: {item_count} record(s) retrieved. Sources: {citation_ids}."
        findings.append(finding)
        trace_entries.append(TraceEntry(stage="ANALYZE", detail=finding))

    logger.info({"event": "analyze_complete", "findings": len(findings)})
    return {"findings": findings, "trace": trace_entries}


def _normalize_comparisons(comparisons: list[Comparison], dimensions: list[str], want_len: int) -> list[Comparison]:
    by_dimension = {c.dimension.strip().lower(): c for c in comparisons}
    normalized: list[Comparison] = []
    for dimension in dimensions:
        existing = by_dimension.get(dimension.strip().lower())
        findings_src = list(existing.findings) if existing else []
        sources = list(existing.sources) if existing else []
        if len(findings_src) < want_len:
            findings_src.extend(["Not found in evidence."] * (want_len - len(findings_src)))
        elif len(findings_src) > want_len:
            findings_src = findings_src[:want_len]
        normalized.append(Comparison(dimension=dimension, findings=findings_src, sources=sources))
    return normalized


def _comparison_options(state: AnalysisState) -> list[str]:
    options: list[str] = []
    focal = state.get("focal_entity")
    if focal:
        options.append(focal)
    for competitor in state.get("competitors", []):
        if competitor not in options:
            options.append(competitor)
    if len(options) < 2:
        options.append("Other market participants")
    return options[:6]


def compare(state: AnalysisState) -> dict[str, Any]:
    """Build Comparison objects using a fixed framework per analysis type."""
    analysis_type: AnalysisType = state["analysis_type"]
    mode = state.get("mode", "science")
    dimensions = list(_FIXED_DIMENSIONS[analysis_type])
    options = _comparison_options(state)
    if analysis_type == AnalysisType.COMPETITOR and mode == "business":
        dimensions.append(f"SWOT ({state.get('focal_entity') or options[0]})")
    want_len = len(options) if analysis_type == AnalysisType.COMPETITOR else 1

    loader = PromptLoader(agent_templates_dir=_ANALYSIS_TEMPLATES_DIR)
    system_prompt = loader.render(
        _COMPARE_TEMPLATE[analysis_type],
        options=options,
        mode=mode,
        focal_entity=state.get("focal_entity"),
        market=state.get("market"),
        jurisdiction=state.get("jurisdiction"),
    )
    findings_text = "\n".join(state.get("findings", [])) or "(no findings gathered)"

    parsed, raw, error = invoke_structured(
        get_model(),
        ComparisonsOutput,
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Findings:\n{findings_text}"},
        ],
    )

    trace_entries: list[TraceEntry] = []
    if parsed is not None:
        comparisons = _normalize_comparisons(parsed.comparisons, dimensions, want_len)
        in_tok, out_tok = extract_tokens(raw) if raw is not None else (0, 0)
        trace_entries.append(TraceEntry(
            stage="COMPARE",
            detail=f"type={analysis_type.value} | dimensions={len(comparisons)}",
            input_tokens=in_tok,
            output_tokens=out_tok,
        ))
    else:
        comparisons = _normalize_comparisons([], dimensions, want_len)
        trace_entries.append(TraceEntry(stage="COMPARE_FALLBACK", detail=f"error={error}"))

    logger.info({"event": "compare_complete", "dimensions": len(comparisons)})
    return {"comparisons": comparisons, "trace": trace_entries}


def _comparison_to_block(comparisons: list[Comparison], options: list[str]) -> ComparisonBlock | None:
    if len(options) < 2:
        return None
    criteria = [
        ComparisonCriterionItem(label=c.dimension, values=list(c.findings))
        for c in comparisons
        if len(c.findings) == len(options)
    ]
    if not criteria:
        return None
    return ComparisonBlock(kind="comparison", title="Comparison matrix", options=options, criteria=criteria)


def _comparison_to_table(comparisons: list[Comparison]) -> TableBlock:
    rows = [[c.dimension, c.findings[0] if c.findings else "Not found in evidence."] for c in comparisons]
    return TableBlock(kind="table", title="Analysis matrix", columns=["Dimension", "Finding"], rows=rows)


def report(state: AnalysisState) -> dict[str, Any]:
    """Produce the final grounded ResponseDocument, falling back to prose when structured output fails."""
    loader = PromptLoader(agent_templates_dir=_ANALYSIS_TEMPLATES_DIR)
    mode = state.get("mode", "science")
    analysis_type: AnalysisType = state["analysis_type"]
    query = state.get("query", "")

    context_parts: list[str] = []
    evidence_texts: list[str] = []
    for res in state.get("retrieved_results", []):
        context_parts.append(f"### {res.source_type.value.upper()} RESULTS")
        for item in res.results:
            context_parts.append(str(item))
            evidence_texts.append(str(item))
        for cit in res.citations:
            context_parts.append(f"[{cit.source_id}] {cit.title} ({cit.year}) — {cit.url}")
            evidence_texts.append(cit.title)
            if cit.relevant_passage:
                evidence_texts.append(cit.relevant_passage)

    comparisons = state.get("comparisons", [])
    comparison_text = "\n".join(
        f"- {c.dimension}: {' | '.join(c.findings)}" for c in comparisons
    )
    context = "\n".join(context_parts)

    structured_system_prompt = loader.system_prompt(mode, analysis_type=analysis_type.value, structured_blocks=True)
    structured_user_prompt = (
        f"Query: {query}\n\nComparison matrix:\n{comparison_text}\n\nEvidence:\n{context}\n\n"
        "Compose the answer as a visual report with inline [SourceID] citations. Include the comparison "
        "matrix as a ComparisonBlock or TableBlock. Lead with graphics and structured blocks; use prose "
        "only where explanation is required."
    )
    parsed, raw, error = invoke_structured(
        get_model(max_tokens=_SYNTHESIS_MAX_TOKENS),
        ResponseDocument,
        [
            {"role": "system", "content": structured_system_prompt},
            {"role": "user", "content": structured_user_prompt},
        ],
    )

    trace_entries: list[TraceEntry] = []
    options = _comparison_options(state)

    if parsed is not None:
        try:
            has_matrix = any(
                isinstance(block, (ComparisonBlock, TableBlock))
                for section in parsed.sections
                for block in section.blocks
            )
            if not has_matrix and comparisons:
                block = _comparison_to_block(comparisons, options) or _comparison_to_table(comparisons)
                parsed.sections[0].blocks.append(block)

            corpus = build_corpus(query, comparison_text, *context_parts, *evidence_texts)
            doc, notes = ground_document(parsed, corpus)
            report_text = render_document_markdown(doc)
            in_tok, out_tok = extract_tokens(raw) if raw is not None else (0, 0)
            trace_entries.append(TraceEntry(
                stage="REPORT",
                detail=f"mode={mode} | structured=true | response_length={len(report_text)}",
                input_tokens=in_tok,
                output_tokens=out_tok,
            ))
            trace_entries.extend(TraceEntry(stage="GROUNDING", detail=note) for note in notes)
            logger.info({"event": "report_complete", "length": len(report_text), "structured": True})
            return {
                "report": report_text,
                "response_document": doc,
                "messages": [AIMessage(content=report_text)],
                "trace": trace_entries,
            }
        except Exception as exc:  # ADR: any render/grounding failure falls back to prose | never lose the answer
            logger.error({"event": "report_structured_render_error", "error_type": type(exc).__name__})
            error = type(exc).__name__

    trace_entries.append(TraceEntry(stage="REPORT_FALLBACK", detail=f"mode={mode} | error={error}"))

    system_prompt = loader.system_prompt(mode, analysis_type=analysis_type.value)
    model = get_model()
    response = model.invoke([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Query: {query}\n\nComparison matrix:\n{comparison_text}\n\nEvidence:\n{context}"},
    ])
    report_text = str(response.content)
    in_tok, out_tok = extract_tokens(response)
    trace_entries.append(TraceEntry(
        stage="REPORT",
        detail=f"mode={mode} | response_length={len(report_text)}",
        input_tokens=in_tok,
        output_tokens=out_tok,
    ))
    logger.info({"event": "report_complete", "length": len(report_text)})
    return {
        "report": report_text,
        "response_document": None,
        "messages": [AIMessage(content=report_text)],
        "trace": trace_entries,
    }
