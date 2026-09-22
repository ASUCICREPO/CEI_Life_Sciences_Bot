from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage
from pydantic import BaseModel, ConfigDict

from drafting.state import DocumentType, DraftingState, Guideline
from shared.prompts.loader import PromptLoader
from shared.schemas.citation import SourceType
from shared.config import config
from shared.state import TraceEntry
from shared.tools.chart import ChartSpec, ChartTool
from shared.tools.code_interpreter import CodeInterpreterTool
from shared.tools.data_sources import ALL_DATA_SOURCE_TOOLS
from shared.tools.export import ExportTool
from shared.utils import extract_tokens, get_model, last_human_message

logger = logging.getLogger(__name__)

_DRAFTING_TEMPLATES_DIR = Path(__file__).parent / "prompts" / "templates"

_GUIDELINE_SOURCES: dict[DocumentType, list[SourceType]] = {
    DocumentType.GRANT: [SourceType.PUBMED, SourceType.CLINICAL_TRIALS, SourceType.CMS_COVERAGE],
    DocumentType.VC_PITCH: [SourceType.PUBMED, SourceType.OPEN_TARGETS, SourceType.CHEMBL],
    DocumentType.JOURNAL: [SourceType.PUBMED, SourceType.BIORXIV, SourceType.MEDRXIV],
    DocumentType.WHITEPAPER: [SourceType.PUBMED, SourceType.OPEN_TARGETS, SourceType.PUBCHEM],
    DocumentType.LATEX: [SourceType.PUBMED, SourceType.BIORXIV, SourceType.MEDRXIV],
}


class GuidelinePlan(BaseModel):
    model_config = ConfigDict(frozen=True)
    source_queries: dict[str, str] = {}
    guideline_focus: str
    reasoning: str


class GuidelinesExtraction(BaseModel):
    model_config = ConfigDict(frozen=True)
    guidelines: list[Guideline]


_GUIDELINES_TRIAGE_SYSTEM = """\
You are a guideline research assistant for a life sciences document drafting bot. Given a document type and user request, generate optimized search queries for each data source to find submission guidelines, formatting requirements, and best practices.

RULES:
1. Focus on SUBMISSION GUIDELINES and FORMATTING REQUIREMENTS, not general research content.
2. For each source, write an optimized query tailored to that source's API:
   - PubMed: search for author guidelines, reporting standards (CONSORT, PRISMA, STROBE)
   - ClinicalTrials.gov: search for registration requirements, reporting standards
   - CMS Coverage: search for coverage determination criteria, submission requirements
   - ChEMBL/PubChem: search for data submission formats, compound registration standards
   - bioRxiv/medRxiv: search for preprint formatting, submission requirements
   - Open Targets: search for evidence submission standards, data integration formats
3. Also identify what specific aspects of the guidelines to focus on (guideline_focus).

Respond ONLY with valid JSON matching this schema:
{
  "source_queries": {"source_key": "optimized query", ...},
  "guideline_focus": "what specific guidelines to prioritize",
  "reasoning": "brief explanation of query strategy"
}"""


_GUIDELINES_EXTRACTION_SYSTEM = """\
Extract submission guidelines and formatting requirements from the following evidence. For each guideline found, provide:
- source: the name of the source or standard (e.g., "NIH R01 Guidelines", "CONSORT Statement")
- requirement: the specific requirement or formatting rule
- url: a URL to the guideline source if available, otherwise null

Return ONLY guidelines relevant to preparing a {doc_type} document. If no specific guidelines are found in the evidence, extract general best practices for this document type."""


def research_guidelines(state: DraftingState) -> dict[str, Any]:
    """Look up latest guidelines and requirements for the target document type."""
    query = last_human_message(state)
    doc_type = state["document_type"]
    sources = _GUIDELINE_SOURCES.get(doc_type, list(SourceType))

    # LLM call 1: generate optimized search queries per source
    source_list = ", ".join(s.value for s in sources)
    user_prompt = (
        f"Document type: {doc_type.value}\n"
        f"User request: {query}\n"
        f"Available sources: {source_list}"
    )

    model = get_model().with_structured_output(GuidelinePlan, include_raw=True)
    raw_result = model.invoke([
        {"role": "system", "content": _GUIDELINES_TRIAGE_SYSTEM},
        {"role": "user", "content": user_prompt},
    ])
    plan: GuidelinePlan = raw_result["parsed"]
    in_tok, out_tok = extract_tokens(raw_result["raw"])

    trace_entries: list[TraceEntry] = [
        TraceEntry(
            stage="GUIDELINE_TRIAGE",
            detail=f"doc_type={doc_type.value} | focus={plan.guideline_focus} | reasoning={plan.reasoning}",
            input_tokens=in_tok,
            output_tokens=out_tok,
        ),
    ]

    # Retrieve from data source tools
    tool_map = {t.source_type: t for t in ALL_DATA_SOURCE_TOOLS}
    context_parts: list[str] = []

    for source in sources:
        tool = tool_map.get(source)
        if tool is None:
            continue
        search_query = plan.source_queries.get(source.value, query)
        try:
            result = tool.invoke(query=search_query)
            for item in result.results:
                context_parts.append(str(item))
            for cit in result.citations:
                context_parts.append(f"[{cit.source_id}] {cit.title} ({cit.year}) — {cit.url}")
            trace_entries.append(TraceEntry(
                stage="TOOL_CALL",
                detail=f"{source.value} | query={search_query!r} | results={len(result.results)}",
            ))
            logger.info({"event": "guideline_retrieve_success", "source": source.value, "count": len(result.results)})
        except Exception:
            logger.exception({"event": "guideline_retrieve_error", "source": source.value})
            trace_entries.append(TraceEntry(
                stage="TOOL_CALL",
                detail=f"{source.value} | query={search_query!r} | ERROR",
            ))

    # LLM call 2: extract structured guidelines from raw results
    context = "\n".join(context_parts)
    extraction_system = _GUIDELINES_EXTRACTION_SYSTEM.format(doc_type=doc_type.value)
    extraction_input = context if context else "No evidence retrieved. Use your knowledge of standard guidelines for this document type."

    extraction_model = get_model().with_structured_output(GuidelinesExtraction, include_raw=True)
    extraction_result = extraction_model.invoke([
        {"role": "system", "content": extraction_system},
        {"role": "user", "content": extraction_input},
    ])
    guidelines = extraction_result["parsed"].guidelines
    ext_in, ext_out = extract_tokens(extraction_result["raw"])

    trace_entries.append(TraceEntry(
        stage="GUIDELINE_EXTRACTION",
        detail=f"extracted={len(guidelines)} guidelines",
        input_tokens=ext_in,
        output_tokens=ext_out,
    ))

    logger.info({"event": "research_guidelines_complete", "doc_type": doc_type.value, "guidelines": len(guidelines)})
    return {"guidelines": guidelines, "trace": trace_entries}


def draft(state: DraftingState) -> dict[str, Any]:
    """Generate the initial document draft using gathered guidelines and sources."""
    query = last_human_message(state)
    mode = state.get("mode", "science")
    doc_type = state["document_type"]
    guidelines = state.get("guidelines", [])

    loader = PromptLoader(agent_templates_dir=_DRAFTING_TEMPLATES_DIR)
    system_prompt = loader.system_prompt(
        mode,
        document_type=doc_type.value,
        guidelines=guidelines,
    )

    guidelines_text = "\n".join(f"- [{g.source}] {g.requirement}" for g in guidelines)
    if guidelines_text:
        user_prompt = (
            f"Request: {query}\n\n"
            f"Guidelines to follow:\n{guidelines_text}\n\n"
            f"Generate a complete {doc_type.value} document draft. "
            f"Follow all listed guidelines. Use [SourceID] notation for citations."
        )
    else:
        user_prompt = (
            f"Request: {query}\n\n"
            f"Generate a complete {doc_type.value} document draft."
        )

    model = get_model()
    response = model.invoke([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ])
    draft_text = str(response.content)
    in_tok, out_tok = extract_tokens(response)

    trace = TraceEntry(
        stage="DRAFT",
        detail=f"doc_type={doc_type.value} | mode={mode} | length={len(draft_text)} | guidelines={len(guidelines)}",
        input_tokens=in_tok,
        output_tokens=out_tok,
    )

    logger.info({"event": "draft_complete", "doc_type": doc_type.value, "length": len(draft_text)})
    return {
        "draft": draft_text,
        "messages": [AIMessage(content=draft_text)],
        "trace": [trace],
    }


def revise(state: DraftingState) -> dict[str, Any]:
    """Incorporate user feedback and refine the draft."""
    mode = state.get("mode", "science")
    doc_type = state["document_type"]
    guidelines = state.get("guidelines", [])
    current_draft = state.get("draft", "")
    feedback = state.get("revision_feedback", "")

    loader = PromptLoader(agent_templates_dir=_DRAFTING_TEMPLATES_DIR)
    system_prompt = loader.system_prompt(
        mode,
        document_type=doc_type.value,
        guidelines=guidelines,
    )

    guidelines_text = "\n".join(f"- [{g.source}] {g.requirement}" for g in guidelines)
    user_prompt = (
        f"Current draft:\n{current_draft}\n\n"
        f"Revision feedback:\n{feedback}\n\n"
        f"Guidelines to maintain:\n{guidelines_text}\n\n"
        f"Revise the draft to address the feedback while maintaining compliance "
        f"with all guidelines. Use [SourceID] notation for citations."
    )

    model = get_model()
    response = model.invoke([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ])
    revised_text = str(response.content)
    in_tok, out_tok = extract_tokens(response)

    trace = TraceEntry(
        stage="REVISE",
        detail=f"feedback_length={len(feedback)} | revised_length={len(revised_text)}",
        input_tokens=in_tok,
        output_tokens=out_tok,
    )

    logger.info({"event": "revise_complete", "length": len(revised_text)})
    return {
        "draft": revised_text,
        "revision_feedback": None,
        "messages": [AIMessage(content=revised_text)],
        "trace": [trace],
    }


def should_revise(state: DraftingState) -> str:
    """Route to revision if the user provided feedback, otherwise finalize."""
    if state.get("revision_feedback"):
        return "revise"
    return "end"


def plan_visuals(state: DraftingState) -> dict[str, Any]:
    """Analyze the draft for chart opportunities; fetch data if needed."""
    draft_text = state.get("draft", "")
    export_formats = state.get("export_formats", [])
    existing_chart_data: dict[str, Any] = state.get("chart_data", {}) or {}

    # Skip visuals for text-only formats
    rich_formats = {f for f in export_formats if f in {"pdf", "docx"}}
    if not rich_formats:
        trace = TraceEntry(stage="PLAN_VISUALS", detail="Skipped — no rich export formats requested")
        return {"chart_specs": [], "trace": [trace]}

    # LLM call: detect chart opportunities in the draft
    class _ChartOpportunity(BaseModel):
        title: str
        chart_type: str  # line, bar, scatter, pie
        description: str
        data_needed: str
        x_label: str = ""
        y_label: str = ""

    class _ChartOpportunities(BaseModel):
        charts: list[_ChartOpportunity]

    _CHART_DETECTION_SYSTEM = """\
Analyze the following document draft and identify places where a data visualization (chart or graph) would significantly enhance the document. For each opportunity, specify:
- title: a short descriptive title for the chart
- chart_type: one of line, bar, scatter, pie
- description: what the chart should show
- data_needed: what data is needed to generate this chart (be specific about units, time ranges, sources)
- x_label: label for x-axis (if applicable)
- y_label: label for y-axis (if applicable)

Only identify genuinely valuable charts — not decorative ones. Return an empty list if no charts would meaningfully improve the document."""

    model = get_model().with_structured_output(_ChartOpportunities, include_raw=True)
    raw = model.invoke([
        {"role": "system", "content": _CHART_DETECTION_SYSTEM},
        {"role": "user", "content": f"Document draft:\n\n{draft_text}"},
    ])
    opportunities: list[_ChartOpportunity] = raw["parsed"].charts
    in_tok, out_tok = extract_tokens(raw["raw"])

    trace_entries: list[TraceEntry] = [
        TraceEntry(
            stage="PLAN_VISUALS",
            detail=f"detected={len(opportunities)} chart opportunities",
            input_tokens=in_tok,
            output_tokens=out_tok,
        )
    ]

    if not opportunities:
        return {"chart_specs": [], "trace": trace_entries}

    # For each chart opportunity, try to get data
    chart_specs: list[dict] = []
    updated_chart_data: dict[str, Any] = dict(existing_chart_data)
    missing_data_descriptions: list[str] = []

    tool_map = {t.source_type: t for t in ALL_DATA_SOURCE_TOOLS}

    for opp in opportunities:
        if opp.title in updated_chart_data:
            # User already provided data
            chart_specs.append({
                "title": opp.title,
                "chart_type": opp.chart_type,
                "x_label": opp.x_label,
                "y_label": opp.y_label,
                "data": updated_chart_data[opp.title],
            })
            continue

        # Try to extract numeric data from draft text
        extracted = _extract_numeric_data_from_draft(draft_text, opp.title)
        if extracted:
            updated_chart_data[opp.title] = extracted
            chart_specs.append({
                "title": opp.title,
                "chart_type": opp.chart_type,
                "x_label": opp.x_label,
                "y_label": opp.y_label,
                "data": extracted,
            })
            trace_entries.append(TraceEntry(stage="CHART_DATA", detail=f"'{opp.title}' — extracted from draft"))
            continue

        # Try data source tools
        fetched = _fetch_chart_data_from_tools(opp.data_needed, tool_map)
        if fetched:
            updated_chart_data[opp.title] = fetched
            chart_specs.append({
                "title": opp.title,
                "chart_type": opp.chart_type,
                "x_label": opp.x_label,
                "y_label": opp.y_label,
                "data": fetched,
            })
            trace_entries.append(TraceEntry(stage="CHART_DATA", detail=f"'{opp.title}' — fetched from data sources"))
        else:
            missing_data_descriptions.append(f"'{opp.title}': {opp.data_needed}")
            trace_entries.append(TraceEntry(stage="CHART_DATA", detail=f"'{opp.title}' — data not found"))

    chart_instructions = None
    if missing_data_descriptions:
        chart_instructions = (
            "The following charts could not be generated automatically because data was not found. "
            "Please provide the data or indicate whether to skip these charts:\n"
            + "\n".join(f"  - {d}" for d in missing_data_descriptions)
        )
        logger.info({"event": "chart_data_missing", "charts": missing_data_descriptions})

    logger.info({"event": "plan_visuals_complete", "charts_ready": len(chart_specs), "missing": len(missing_data_descriptions)})
    return {
        "chart_specs": chart_specs,
        "chart_data": updated_chart_data,
        "chart_instructions": chart_instructions,
        "trace": trace_entries,
    }


def _extract_numeric_data_from_draft(draft: str, chart_title: str) -> dict[str, Any] | None:
    """Try to find a markdown table in the draft that matches the chart title."""
    import re
    # Look for a table near the chart title mention
    title_pattern = re.compile(re.escape(chart_title), re.IGNORECASE)
    lines = draft.splitlines()
    for i, line in enumerate(lines):
        if title_pattern.search(line):
            # Look for a markdown table in the next 20 lines
            table_lines = [l for l in lines[i:i+20] if "|" in l and not l.strip().startswith("|---")]
            if len(table_lines) >= 2:
                headers = [h.strip() for h in table_lines[0].strip("|").split("|")]
                data: dict[str, list] = {h: [] for h in headers}
                for row in table_lines[1:]:
                    cells = [c.strip() for c in row.strip("|").split("|")]
                    for j, h in enumerate(headers):
                        if j < len(cells):
                            try:
                                data[h].append(float(cells[j]))
                            except ValueError:
                                data[h].append(cells[j])
                if any(isinstance(v[0], float) for v in data.values() if v):
                    return data
    return None


def _fetch_chart_data_from_tools(data_needed: str, tool_map: dict) -> dict[str, Any] | None:
    """Try to fetch chart-relevant data from life sciences data source tools."""
    from shared.schemas.citation import SourceType
    # Use PubMed for general data queries
    pubmed_tool = tool_map.get(SourceType.PUBMED)
    if pubmed_tool is None:
        return None
    try:
        result = pubmed_tool.invoke(query=data_needed)
        if not result.results:
            return None
        # Build a simple bar chart from result counts by year if possible
        years: dict[str, int] = {}
        for item in result.results:
            year = str(item.get("year", "Unknown")) if isinstance(item, dict) else "Unknown"
            years[year] = years.get(year, 0) + 1
        if len(years) >= 2:
            sorted_years = sorted(years.items())
            return {"x": [y for y, _ in sorted_years], "Publications": [c for _, c in sorted_years]}
    except Exception:
        logger.exception({"event": "fetch_chart_data_error"})
    return None


def export_documents(state: DraftingState) -> dict[str, Any]:
    """Render charts and export the draft to all requested formats."""
    export_formats = state.get("export_formats", ["md", "txt"])
    draft_text = state.get("draft", "")
    chart_specs_raw: list[dict] = state.get("chart_specs", [])
    doc_type = state.get("document_type", "document")
    doc_type_str = doc_type.value if hasattr(doc_type, "value") else str(doc_type)

    # Derive title from first heading in draft or fallback
    import re
    title_match = re.search(r"^#\s+(.+)$", draft_text, re.MULTILINE)
    title = title_match.group(1).strip() if title_match else f"{doc_type_str}_document"

    # Build sections from draft headings
    sections: list[dict] = []
    current_heading = ""
    current_content_lines: list[str] = []
    order = 0
    for line in draft_text.splitlines():
        if line.startswith("## "):
            if current_content_lines:
                sections.append({"heading": current_heading, "content": "\n".join(current_content_lines).strip(), "order": order})
                order += 1
                current_content_lines = []
            current_heading = line[3:].strip()
        elif line.startswith("# "):
            pass  # Skip document title line
        else:
            current_content_lines.append(line)
    if current_content_lines:
        sections.append({"heading": current_heading, "content": "\n".join(current_content_lines).strip(), "order": order})

    trace_entries: list[TraceEntry] = []

    # Render charts if any rich formats requested
    rendered_charts: dict[str, bytes] = {}
    rich_formats = {f for f in export_formats if f in {"pdf", "docx"}}
    if rich_formats and chart_specs_raw:
        ci_tool = CodeInterpreterTool(
            region=config.aws_region,
            code_interpreter_id=config.code_interpreter_id,
            packages_s3_bucket=config.packages_s3_bucket,
            packages_s3_key=config.packages_s3_key,
        )
        chart_tool = ChartTool(ci_tool)
        for spec_dict in chart_specs_raw:
            try:
                spec = ChartSpec(**spec_dict)
                png_bytes = chart_tool.render(spec)
                if png_bytes:
                    rendered_charts[spec.title] = png_bytes
                    trace_entries.append(TraceEntry(stage="CHART_RENDER", detail=f"'{spec.title}' — {len(png_bytes)} bytes"))
                else:
                    trace_entries.append(TraceEntry(stage="CHART_RENDER", detail=f"'{spec.title}' — failed"))
            except Exception:
                logger.exception({"event": "chart_render_error", "title": spec_dict.get("title")})

    # Determine output directory
    results_dir = Path(__file__).parents[2] / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    # Generate exports
    needs_ci = any(f in {"pdf", "docx"} for f in export_formats)
    if needs_ci and not rendered_charts:
        # Still need CI for PDF/DOCX even without charts
        ci_tool = CodeInterpreterTool(
            region=config.aws_region,
            code_interpreter_id=config.code_interpreter_id,
            packages_s3_bucket=config.packages_s3_bucket,
            packages_s3_key=config.packages_s3_key,
        )
    elif not rich_formats:
        ci_tool = None

    doc_type_str = doc_type.value if hasattr(doc_type, "value") else str(doc_type)
    export_tool = ExportTool(
        code_interpreter=ci_tool if needs_ci else CodeInterpreterTool(
            region=config.aws_region,
            code_interpreter_id=config.code_interpreter_id,
        ),
        doc_type=doc_type_str,
    )
    artifacts = export_tool.generate(
        title=title,
        sections=sections,
        charts=rendered_charts,
        formats=export_formats,
        output_dir=results_dir,
    )

    for art in artifacts:
        trace_entries.append(TraceEntry(
            stage="EXPORT",
            detail=f"{art.format.upper()} → {art.filename} ({art.byte_size} bytes)",
        ))

    export_summary = ", ".join(f"{a.format}:{a.filename}" for a in artifacts)
    logger.info({"event": "export_documents_complete", "exports": export_summary})

    return {
        "exports": [a.model_dump() for a in artifacts],
        "trace": trace_entries,
    }
