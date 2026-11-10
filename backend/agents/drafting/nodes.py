from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage
from langgraph.types import interrupt
from pydantic import BaseModel, ConfigDict

from drafting.state import DocumentType, DraftingState, Guideline
from shared.grounding import build_corpus, ground_document, is_grounded
from shared.presenters.markdown import document_sections_for_export, render_document_markdown
from shared.prompts.loader import PromptLoader
from shared.schemas.citation import SourceType
from shared.schemas.cei_ui import (
    ActionDescriptor,
    CEIUISurface,
    ColumnComponent,
    MAX_PLAN_STEPS,
    Plan,
    PlanStep,
    ReviewGateComponent,
    TextComponent,
    TextFieldComponent,
)
from shared.schemas.response_blocks import ResponseDocument
from shared.state import TraceEntry
from shared.structured import invoke_structured
from shared.tools.chart import place_charts
from shared.tools.data_sources import ALL_DATA_SOURCE_TOOLS
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


_VISUALS_RULE = (
    "Charts come from chart blocks, not from code. Do NOT write plotting code (matplotlib, seaborn, plotly, R, "
    "or any other) and do NOT describe how to make a chart. If you are returning a ResponseDocument, put "
    "chart-worthy series in a chart block. If you are writing plain text, put the numbers in a markdown table "
    "under the relevant '## ' heading instead."
)

_DRAFT_MAX_TOKENS = 16384

# ChartTool has no horizontal-bar renderer; render it as a vertical bar chart instead.
_EXPORT_CHART_TYPE_MAP = {"bar-horizontal": "bar"}

_PLOT_CODE_BLOCK = re.compile(
    r"```[^\n]*\n(?:(?!```).)*?(?:matplotlib|pyplot|plt\.|seaborn|plotly|ggplot)(?:(?!```).)*?```\n?",
    re.DOTALL,
)


def _strip_plot_code(text: str) -> str:
    """Remove fenced code blocks that contain plotting code; charts are rendered by the export step."""
    return _PLOT_CODE_BLOCK.sub("", text)


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
        f"Document type: {doc_type.value}\nUser request: {query}\nAvailable sources: {source_list}"
    )

    model = get_model().with_structured_output(GuidelinePlan, include_raw=True)
    raw_result = model.invoke(
        [
            {"role": "system", "content": _GUIDELINES_TRIAGE_SYSTEM},
            {"role": "user", "content": user_prompt},
        ]
    )
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
            trace_entries.append(
                TraceEntry(
                    stage="TOOL_CALL",
                    detail=f"{source.value} | query={search_query!r} | results={len(result.results)}",
                )
            )
            logger.info(
                {
                    "event": "guideline_retrieve_success",
                    "source": source.value,
                    "count": len(result.results),
                }
            )
        except Exception as exc:
            logger.error(
                {
                    "event": "guideline_retrieve_error",
                    "source": source.value,
                    "error_type": type(exc).__name__,
                }
            )
            trace_entries.append(
                TraceEntry(
                    stage="TOOL_CALL",
                    detail=f"{source.value} | query={search_query!r} | ERROR",
                )
            )

    # LLM call 2: extract structured guidelines from raw results
    context = "\n".join(context_parts)
    extraction_system = _GUIDELINES_EXTRACTION_SYSTEM.format(doc_type=doc_type.value)
    extraction_input = (
        context
        if context
        else "No evidence retrieved. Use your knowledge of standard guidelines for this document type."
    )

    extraction_model = get_model().with_structured_output(GuidelinesExtraction, include_raw=True)
    extraction_result = extraction_model.invoke(
        [
            {"role": "system", "content": extraction_system},
            {"role": "user", "content": extraction_input},
        ]
    )
    guidelines = extraction_result["parsed"].guidelines
    ext_in, ext_out = extract_tokens(extraction_result["raw"])

    trace_entries.append(
        TraceEntry(
            stage="GUIDELINE_EXTRACTION",
            detail=f"extracted={len(guidelines)} guidelines",
            input_tokens=ext_in,
            output_tokens=ext_out,
        )
    )

    logger.info(
        {
            "event": "research_guidelines_complete",
            "doc_type": doc_type.value,
            "guidelines": len(guidelines),
        }
    )
    return {"guidelines": guidelines, "trace": trace_entries}


_MAX_RESEARCH_QUESTIONS = 3

_RESEARCH_PLAN_SYSTEM = """\
You decide whether a life sciences document needs external evidence before it is drafted. A Research Agent is available that searches these sources and returns a cited answer:
{sources}

Set needs_research=true when the document must make factual claims about science, clinical evidence, trials, targets, compounds, coverage policy, or the state of a field. Set needs_research=false only when the user supplied all the facts needed, or the request is purely about structure or formatting.

If research is needed, write 1 to {max_q} focused research questions. Each question must cover a different key claim the document must support. Write each question so it can be answered from the sources above. Do not ask about submission guidelines or formatting."""


class _ResearchPlan(BaseModel):
    needs_research: bool
    questions: list[str] = []
    reasoning: str


def plan_research(state: DraftingState) -> dict[str, Any]:
    """Decide if the document needs evidence from the Research Agent, and what to ask it."""
    from research.nodes import _SOURCE_DESCRIPTIONS

    query = last_human_message(state)
    doc_type = state["document_type"]
    sources = "\n".join(f"- {v}" for v in _SOURCE_DESCRIPTIONS.values())

    model = get_model().with_structured_output(_ResearchPlan, include_raw=True)
    raw = model.invoke(
        [
            {
                "role": "system",
                "content": _RESEARCH_PLAN_SYSTEM.format(
                    sources=sources, max_q=_MAX_RESEARCH_QUESTIONS
                ),
            },
            {"role": "user", "content": f"Document type: {doc_type.value}\nUser request: {query}"},
        ]
    )
    plan: _ResearchPlan = raw["parsed"]
    in_tok, out_tok = extract_tokens(raw["raw"])

    questions = (
        [q.strip() for q in plan.questions if q.strip()][:_MAX_RESEARCH_QUESTIONS]
        if plan.needs_research
        else []
    )
    trace = TraceEntry(
        stage="PLAN_RESEARCH",
        detail=f"needs_research={bool(questions)} | questions={len(questions)} | reasoning={plan.reasoning}",
        input_tokens=in_tok,
        output_tokens=out_tok,
    )
    logger.info({"event": "plan_research_complete", "question_count": len(questions)})
    return {"research_questions": questions, "trace": [trace]}


def should_research(state: DraftingState) -> str:
    """Spawn the Research Agent only when plan_research asked questions."""
    return "research" if state.get("research_questions") else "draft"


def research_subagent(state: DraftingState) -> dict[str, Any]:
    """Run the Research Agent graph in-process for each question, in parallel."""
    from langchain_core.messages import HumanMessage

    from research.graph import compile_graph as compile_research_graph

    questions = state.get("research_questions", [])
    session_id = state.get("session_id")
    if (
        not session_id
    ):  # no default session id: sub-research threads derive from the run's own session UUID
        raise ValueError("drafting state has no session_id")
    inputs = [
        {
            "messages": [HumanMessage(content=q)],
            "mode": state.get("mode", "science"),
            "user_id": state.get("user_id", "cli-user"),
            "session_id": f"{session_id}:research:{i}",
            "query": q,
            "triage_plan": None,
            "retrieved_results": [],
            "synthesis": "",
            "trace": [],
            # ADR: skip B72's clarify pause | no interactive user, no checkpointer to pause against
            "clarified": True,
            "clarification_answer": None,
        }
        for i, q in enumerate(questions)
    ]

    # No checkpointer: each subagent run is stateless and scoped to this drafting run
    graph = compile_research_graph()
    results = graph.batch(
        inputs, config={"max_concurrency": _MAX_RESEARCH_QUESTIONS}, return_exceptions=True
    )

    findings: list[dict] = []
    trace_entries: list[TraceEntry] = []
    for i, (question, result) in enumerate(zip(questions, results), start=1):
        if isinstance(result, Exception):
            logger.error({"event": "research_subagent_error", "error_type": type(result).__name__})
            trace_entries.append(
                TraceEntry(
                    stage="RESEARCH_SUBAGENT", detail=f"#{i} {question!r} — ERROR: {result!r}"
                )
            )
            continue
        messages = result.get("messages", [])
        answer = str(messages[-1].content) if messages else ""
        findings.append({"question": question, "answer": answer})
        # Keep the subagent's own steps and token usage visible in the drafting trace
        for sub in result.get("trace", []):
            trace_entries.append(
                TraceEntry(
                    stage=f"RESEARCH#{i}>{sub.stage}",
                    detail=sub.detail,
                    input_tokens=sub.input_tokens,
                    output_tokens=sub.output_tokens,
                )
            )
        trace_entries.append(
            TraceEntry(
                stage="RESEARCH_SUBAGENT",
                detail=f"#{i} {question!r} — answer_length={len(answer)}",
            )
        )

    logger.info(
        {
            "event": "research_subagent_complete",
            "questions": len(questions),
            "findings": len(findings),
        }
    )
    return {"research_findings": findings, "trace": trace_entries}


def _findings_block(findings: list[dict]) -> str:
    if not findings:
        return ""
    parts = [
        "Research findings from the Research Agent. Use these as the evidence for factual claims. "
        "Cite with the [SourceID] markers exactly as they appear here, and include their references. "
        "Never invent citations; mark any claim not supported here as an assumption."
    ]
    for f in findings:
        parts.append(f"### Question: {f['question']}\n{f['answer']}")
    return "\n\n".join(parts)


_OUTLINE_SYSTEM = """\
You are a drafting assistant preparing a {doc_type} document for a life sciences bot. Propose a short \
outline for the document, before the full draft is written: a Markdown bullet list of section headings, \
each with a one-sentence description of what that section will cover. Do not write the document itself."""


def outline(state: DraftingState) -> dict[str, Any]:
    """Propose a short section outline before the full draft (B72); re-run after an edit."""
    query = last_human_message(state)
    doc_type = state["document_type"]
    guidelines = state.get("guidelines", [])
    findings = state.get("research_findings", [])
    correction = state.get("outline_correction")

    guidelines_text = "\n".join(f"- [{g.source}] {g.requirement}" for g in guidelines)
    user_prompt = f"Document type: {doc_type.value}\nUser request: {query}"
    if guidelines_text:
        user_prompt += f"\n\nGuidelines to follow:\n{guidelines_text}"
    if findings:
        user_prompt += "\n\n" + _findings_block(findings)
    if correction:
        user_prompt += f"\n\nThe user asked for this change to the outline: {correction}"

    model = get_model()
    response = model.invoke(
        [
            {"role": "system", "content": _OUTLINE_SYSTEM.format(doc_type=doc_type.value)},
            {"role": "user", "content": user_prompt},
        ]
    )
    outline_text = str(response.content).strip()
    in_tok, out_tok = extract_tokens(response)
    revision = state.get("outline_revision", 0) + 1

    logger.info({"event": "outline_complete", "doc_type": doc_type.value, "revision": revision})
    return {
        "outline": outline_text,
        "outline_revision": revision,
        "outline_correction": None,
        "trace": [
            TraceEntry(
                stage="OUTLINE",
                detail=f"doc_type={doc_type.value} | revision={revision} | length={len(outline_text)}",
                input_tokens=in_tok,
                output_tokens=out_tok,
            )
        ],
    }


def _build_outline_approval_surface(outline_text: str, *, revision: int) -> dict[str, Any]:
    """ReviewGate approve/reject + TextField edit, modelled on analysis's `build_scope_confirmation_surface`."""
    lines = [line.strip(" -*\t") for line in outline_text.splitlines() if line.strip()]
    steps = [
        PlanStep(step_id=f"outline-{index}", title=line)
        for index, line in enumerate((lines or ["Outline"])[:MAX_PLAN_STEPS], start=1)
    ]
    surface = CEIUISurface(
        surface_id="drafting-outline",
        revision=revision,
        root_id="outline-root",
        components=[
            ColumnComponent(id="outline-root", children=["outline-intro", "outline-review", "outline-edit"]),
            TextComponent(
                id="outline-intro",
                content="Review the proposed outline before the full draft is written, or edit it below.",
            ),
            ReviewGateComponent(
                id="outline-review",
                interaction_id="drafting-outline-review",
                plan=Plan(summary="Proposed document outline", steps=steps),
                actions=[
                    ActionDescriptor(action_id="approve-outline", label="Approve", intent="approve"),
                    ActionDescriptor(action_id="reject-outline", label="Reject", intent="reject"),
                ],
            ),
            TextFieldComponent(
                id="outline-edit",
                interaction_id="drafting-outline-edit",
                label="Edit the outline (free text)",
                placeholder="For example: add a methods section, drop the appendix",
                multiline=True,
                action=ActionDescriptor(action_id="edit-outline", label="Submit edit"),
            ),
        ],
    )
    return surface.model_dump(mode="json")


def approve_outline(state: DraftingState) -> dict[str, Any]:
    """Pause for outline approval before the full draft is generated (B72)."""
    if state.get("skip_outline_approval"):
        # ADR: skip B72's outline pause | non-interactive in-process run, no checkpointer to pause against
        return {"outline_approved": True, "trace": [TraceEntry(stage="OUTLINE_APPROVE", detail="skipped: non-interactive")]}
    outline_text = state.get("outline", "")
    revision = state.get("outline_revision", 1)
    surface = _build_outline_approval_surface(outline_text, revision=revision)
    decision = interrupt(surface)
    action_id = decision.get("action_id") if isinstance(decision, dict) else None

    if action_id == "approve-outline":
        logger.info({"event": "outline_approved"})
        return {"outline_approved": True, "trace": [TraceEntry(stage="OUTLINE_APPROVE", detail="approved")]}

    if action_id == "reject-outline":
        logger.info({"event": "outline_rejected"})
        return {
            "outline_approved": False,
            "outline_correction": None,
            "messages": [AIMessage(content="Drafting cancelled at the user's request.")],
            "trace": [TraceEntry(stage="OUTLINE_APPROVE", detail="rejected")],
        }

    if action_id == "edit-outline":
        value = decision.get("value") if isinstance(decision, dict) else None
        correction = value if isinstance(value, str) else str(value)
        logger.info({"event": "outline_edited"})
        return {
            "outline_approved": False,
            "outline_correction": correction,
            "trace": [TraceEntry(stage="OUTLINE_APPROVE", detail=f"edited: {correction}")],
        }

    logger.warning({"event": "outline_approve_unknown_action", "action_id": action_id})
    return {
        "outline_approved": False,
        "outline_correction": None,
        "messages": [AIMessage(content="Drafting cancelled — the outline approval was not understood.")],
        "trace": [TraceEntry(stage="OUTLINE_APPROVE", detail=f"unknown action: {action_id}")],
    }


def route_after_outline_approval(state: DraftingState) -> str:
    if state.get("outline_approved"):
        return "draft"
    if state.get("outline_correction"):
        return "outline"
    return "end"


def _drafting_corpus(
    base_text: str,
    guidelines: list[Guideline],
    findings: list[dict],
    chart_data: dict[str, Any],
) -> str:
    """Evidence corpus for grounding: the base text, guidelines, research findings, and known chart data."""
    parts: list[str] = [base_text]
    parts.extend(g.requirement for g in guidelines)
    parts.extend(f"{f.get('question', '')} {f.get('answer', '')}" for f in findings)
    for series in chart_data.values():
        if not isinstance(series, dict):
            continue
        for key, values in series.items():
            if key == "x":
                continue
            if isinstance(values, list):
                parts.extend(str(v) for v in values)
            else:
                parts.append(str(values))
    return build_corpus(*parts)


def _chart_specs_from_document(document: ResponseDocument) -> list[dict[str, Any]]:
    """Build chart specs directly from a grounded document's chart blocks — no LLM detection call."""
    specs: list[dict[str, Any]] = []
    for section in document.sections:
        for block in section.blocks:
            if getattr(block, "kind", None) != "chart":
                continue
            data: dict[str, Any] = {"x": list(block.categories)}
            for series in block.series:
                data[series.name] = list(series.values)
            specs.append(
                {
                    "title": block.title,
                    "chart_type": _EXPORT_CHART_TYPE_MAP.get(block.chart_type, block.chart_type),
                    "x_label": block.x_label or "",
                    "y_label": block.y_label or "",
                    "section": section.heading,
                    "data": data,
                }
            )
    return specs


def _render_sections_text(
    title: str,
    sections: list[dict[str, Any]],
    chart_titles: list[str],
    chart_sections: dict[str, str] | None = None,
) -> str:
    """Render sections to markdown text with figure captions, never embedded chart images."""
    placed, unmatched = place_charts(sections, chart_titles, chart_sections)
    lines = [f"# {title}", ""]
    for sec in sorted(sections, key=lambda s: s.get("order", 0)):
        if sec.get("heading"):
            lines.append(f"## {sec['heading']}")
            lines.append("")
        lines.append(sec.get("content", ""))
        lines.append("")
        for chart_title in placed.get(sec.get("order", 0), []):
            lines.append(f"*Figure: {chart_title}*")
            lines.append("")
    if unmatched:
        lines += ["## Figures", ""]
        for chart_title in unmatched:
            lines.append(f"*Figure: {chart_title}*")
            lines.append("")
    return "\n".join(lines)


def draft(state: DraftingState) -> dict[str, Any]:
    """Generate the initial document draft using gathered guidelines and sources."""
    query = last_human_message(state)
    mode = state.get("mode", "science")
    doc_type = state["document_type"]
    guidelines = state.get("guidelines", [])
    findings = state.get("research_findings", [])
    outline_text = state.get("outline", "")

    loader = PromptLoader(agent_templates_dir=_DRAFTING_TEMPLATES_DIR)

    guidelines_text = "\n".join(f"- [{g.source}] {g.requirement}" for g in guidelines)
    if guidelines_text:
        user_prompt = (
            f"Request: {query}\n\n"
            f"Guidelines to follow:\n{guidelines_text}\n\n"
            f"Generate a complete {doc_type.value} document draft. "
            f"Follow all listed guidelines. Use [SourceID] notation for citations.\n\n"
            f"{_VISUALS_RULE}"
        )
    else:
        user_prompt = (
            f"Request: {query}\n\n"
            f"Generate a complete {doc_type.value} document draft.\n\n"
            f"{_VISUALS_RULE}"
        )
    if outline_text:
        user_prompt += f"\n\nFollow this approved outline:\n{outline_text}"
    if findings:
        user_prompt += "\n\n" + _findings_block(findings)

    structured_system_prompt = loader.system_prompt(
        mode,
        document_type=doc_type.value,
        guidelines=guidelines,
        structured_blocks=True,
    )
    parsed, raw, error = invoke_structured(
        get_model(max_tokens=_DRAFT_MAX_TOKENS),
        ResponseDocument,
        [
            {"role": "system", "content": structured_system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )

    trace_entries: list[TraceEntry] = []
    if parsed is not None:
        try:
            corpus = _drafting_corpus(
                query, guidelines, findings, state.get("chart_data", {}) or {}
            )
            doc, notes = ground_document(parsed, corpus)
            draft_text = render_document_markdown(doc)
            in_tok, out_tok = extract_tokens(raw) if raw is not None else (0, 0)
            trace_entries.append(
                TraceEntry(
                    stage="DRAFT",
                    detail=(
                        f"doc_type={doc_type.value} | mode={mode} | structured=true | length={len(draft_text)} "
                        f"| guidelines={len(guidelines)} | research_findings={len(findings)}"
                    ),
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                )
            )
            trace_entries.extend(TraceEntry(stage="GROUNDING", detail=note) for note in notes)
            logger.info(
                {
                    "event": "draft_complete",
                    "doc_type": doc_type.value,
                    "length": len(draft_text),
                    "structured": True,
                }
            )
            return {
                "draft": draft_text,
                "draft_document": doc,
                "messages": [AIMessage(content=draft_text)],
                "trace": trace_entries,
            }
        except (
            Exception
        ) as exc:  # ADR: any render/grounding failure falls back to prose | never lose the draft
            logger.error(
                {"event": "draft_structured_render_error", "error_type": type(exc).__name__}
            )
            error = type(exc).__name__

    trace_entries.append(
        TraceEntry(
            stage="DRAFT_FALLBACK",
            detail=f"doc_type={doc_type.value} | mode={mode} | error={error}",
        )
    )

    # Existing prose path, unchanged.
    system_prompt = loader.system_prompt(mode, document_type=doc_type.value, guidelines=guidelines)
    model = get_model()
    response = model.invoke(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
    )
    draft_text = _strip_plot_code(str(response.content))
    in_tok, out_tok = extract_tokens(response)

    trace_entries.append(
        TraceEntry(
            stage="DRAFT",
            detail=f"doc_type={doc_type.value} | mode={mode} | length={len(draft_text)} | guidelines={len(guidelines)} | research_findings={len(findings)}",
            input_tokens=in_tok,
            output_tokens=out_tok,
        )
    )

    logger.info({"event": "draft_complete", "doc_type": doc_type.value, "length": len(draft_text)})
    return {
        "draft": draft_text,
        "draft_document": None,
        "messages": [AIMessage(content=draft_text)],
        "trace": trace_entries,
    }


def revise(state: DraftingState) -> dict[str, Any]:
    """Incorporate user feedback and refine the draft."""
    mode = state.get("mode", "science")
    doc_type = state["document_type"]
    guidelines = state.get("guidelines", [])
    current_draft = state.get("draft", "")
    feedback = state.get("revision_feedback", "")
    findings = state.get("research_findings", [])

    loader = PromptLoader(agent_templates_dir=_DRAFTING_TEMPLATES_DIR)

    guidelines_text = "\n".join(f"- [{g.source}] {g.requirement}" for g in guidelines)
    user_prompt = (
        f"Current draft:\n{current_draft}\n\n"
        f"Revision feedback:\n{feedback}\n\n"
        f"Guidelines to maintain:\n{guidelines_text}\n\n"
        f"Revise the draft to address the feedback while maintaining compliance "
        f"with all guidelines. Use [SourceID] notation for citations.\n\n"
        f"{_VISUALS_RULE}"
    )
    if findings:
        user_prompt += "\n\n" + _findings_block(findings)

    structured_system_prompt = loader.system_prompt(
        mode,
        document_type=doc_type.value,
        guidelines=guidelines,
        structured_blocks=True,
    )
    parsed, raw, error = invoke_structured(
        get_model(max_tokens=_DRAFT_MAX_TOKENS),
        ResponseDocument,
        [
            {"role": "system", "content": structured_system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )

    trace_entries: list[TraceEntry] = []
    if parsed is not None:
        try:
            corpus = _drafting_corpus(
                f"{current_draft}\n{feedback}",
                guidelines,
                findings,
                state.get("chart_data", {}) or {},
            )
            doc, notes = ground_document(parsed, corpus)
            revised_text = render_document_markdown(doc)
            in_tok, out_tok = extract_tokens(raw) if raw is not None else (0, 0)
            trace_entries.append(
                TraceEntry(
                    stage="REVISE",
                    detail=f"feedback_length={len(feedback)} | structured=true | revised_length={len(revised_text)}",
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                )
            )
            trace_entries.extend(TraceEntry(stage="GROUNDING", detail=note) for note in notes)
            logger.info(
                {"event": "revise_complete", "length": len(revised_text), "structured": True}
            )
            return {
                "draft": revised_text,
                "draft_document": doc,
                "revision_feedback": None,
                "messages": [AIMessage(content=revised_text)],
                "trace": trace_entries,
            }
        except (
            Exception
        ) as exc:  # ADR: any render/grounding failure falls back to prose | never lose the revision
            logger.error(
                {"event": "revise_structured_render_error", "error_type": type(exc).__name__}
            )
            error = type(exc).__name__

    trace_entries.append(
        TraceEntry(
            stage="REVISE_FALLBACK", detail=f"feedback_length={len(feedback)} | error={error}"
        )
    )

    # Existing prose path, unchanged.
    system_prompt = loader.system_prompt(mode, document_type=doc_type.value, guidelines=guidelines)
    model = get_model()
    response = model.invoke(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
    )
    revised_text = _strip_plot_code(str(response.content))
    in_tok, out_tok = extract_tokens(response)

    trace_entries.append(
        TraceEntry(
            stage="REVISE",
            detail=f"feedback_length={len(feedback)} | revised_length={len(revised_text)}",
            input_tokens=in_tok,
            output_tokens=out_tok,
        )
    )

    logger.info({"event": "revise_complete", "length": len(revised_text)})
    return {
        "draft": revised_text,
        "draft_document": None,
        "revision_feedback": None,
        "messages": [AIMessage(content=revised_text)],
        "trace": trace_entries,
    }


def should_revise(state: DraftingState) -> str:
    """Route to revision if the user provided feedback, otherwise finalize."""
    if state.get("revision_feedback"):
        return "revise"
    return "end"


def plan_visuals(state: DraftingState) -> dict[str, Any]:
    """Build chart specs — from the grounded document's chart blocks when one exists, else by LLM detection.

    Specs are always computed (the UI needs them); the export step decides which formats get rendered PNGs.
    """
    document: ResponseDocument | None = state.get("draft_document")
    if document is not None:
        chart_specs = _chart_specs_from_document(document)
        trace = TraceEntry(
            stage="PLAN_VISUALS",
            detail=f"structured=true | charts_from_document={len(chart_specs)}",
        )
        logger.info(
            {"event": "plan_visuals_complete", "charts_ready": len(chart_specs), "structured": True}
        )
        return {"chart_specs": chart_specs, "trace": [trace]}

    draft_text = state.get("draft", "")
    guidelines = state.get("guidelines", [])
    findings = state.get("research_findings", [])
    existing_chart_data: dict[str, Any] = state.get("chart_data", {}) or {}
    corpus = _drafting_corpus(draft_text, guidelines, findings, existing_chart_data)

    # LLM call: detect chart opportunities in the draft
    class _ChartOpportunity(BaseModel):
        title: str
        chart_type: str  # line, bar, scatter, pie
        description: str
        data_needed: str
        x_label: str = ""
        y_label: str = ""
        section_heading: str = ""
        x: list[str] = []
        series: dict[str, list[float]] = {}

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
- section_heading: the exact text of the "## " heading of the section the chart belongs under
- x: the category or x-axis values (for example years), as strings
- series: {series name: list of numbers}, each list the same length as x

Fill x and series ONLY with numbers that are written in the draft. Never invent, estimate, or compute numbers. If the draft does not state the numbers, leave x and series empty.

Only identify genuinely valuable charts — not decorative ones. Return an empty list if no charts would meaningfully improve the document."""

    model = get_model().with_structured_output(_ChartOpportunities, include_raw=True)
    raw = model.invoke(
        [
            {"role": "system", "content": _CHART_DETECTION_SYSTEM},
            {"role": "user", "content": f"Document draft:\n\n{draft_text}"},
        ]
    )
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
            chart_specs.append(
                {
                    "title": opp.title,
                    "chart_type": opp.chart_type,
                    "x_label": opp.x_label,
                    "y_label": opp.y_label,
                    "section": opp.section_heading,
                    "data": updated_chart_data[opp.title],
                }
            )
            continue

        # Use the data the LLM read from the draft, if every number is grounded in the corpus
        grounded = _grounded_chart_data(opp.x, opp.series, corpus)
        if grounded:
            updated_chart_data[opp.title] = grounded
            chart_specs.append(
                {
                    "title": opp.title,
                    "chart_type": opp.chart_type,
                    "x_label": opp.x_label,
                    "y_label": opp.y_label,
                    "section": opp.section_heading,
                    "data": grounded,
                }
            )
            trace_entries.append(
                TraceEntry(stage="CHART_DATA", detail=f"'{opp.title}' — read from draft")
            )
            continue

        # Try to extract numeric data from a markdown table in the draft
        extracted = _extract_numeric_data_from_draft(draft_text, opp.title)
        if not extracted and opp.section_heading:
            extracted = _extract_numeric_data_from_draft(draft_text, opp.section_heading)
        if extracted:
            updated_chart_data[opp.title] = extracted
            chart_specs.append(
                {
                    "title": opp.title,
                    "chart_type": opp.chart_type,
                    "x_label": opp.x_label,
                    "y_label": opp.y_label,
                    "section": opp.section_heading,
                    "data": extracted,
                }
            )
            trace_entries.append(
                TraceEntry(stage="CHART_DATA", detail=f"'{opp.title}' — extracted from draft")
            )
            continue

        # Try data source tools
        fetched = _fetch_chart_data_from_tools(opp.data_needed, tool_map)
        if fetched:
            updated_chart_data[opp.title] = fetched
            chart_specs.append(
                {
                    "title": opp.title,
                    "chart_type": opp.chart_type,
                    "x_label": opp.x_label,
                    "y_label": opp.y_label,
                    "section": opp.section_heading,
                    "data": fetched,
                }
            )
            trace_entries.append(
                TraceEntry(stage="CHART_DATA", detail=f"'{opp.title}' — fetched from data sources")
            )
        else:
            missing_data_descriptions.append(f"'{opp.title}': {opp.data_needed}")
            trace_entries.append(
                TraceEntry(stage="CHART_DATA", detail=f"'{opp.title}' — data not found")
            )

    chart_instructions = None
    if missing_data_descriptions:
        chart_instructions = (
            "The following charts could not be generated automatically because data was not found. "
            "Please provide the data or indicate whether to skip these charts:\n"
            + "\n".join(f"  - {d}" for d in missing_data_descriptions)
        )
        logger.info({"event": "chart_data_missing", "count": len(missing_data_descriptions)})

    logger.info(
        {
            "event": "plan_visuals_complete",
            "charts_ready": len(chart_specs),
            "missing": len(missing_data_descriptions),
        }
    )
    return {
        "chart_specs": chart_specs,
        "chart_data": updated_chart_data,
        "chart_instructions": chart_instructions,
        "trace": trace_entries,
    }


def _grounded_chart_data(
    x: list[str],
    series: dict[str, list[float]],
    corpus: str,
) -> dict[str, Any] | None:
    """Accept LLM-read chart data only if the shape is valid and every number is grounded in the corpus.

    # ADR: number-boundary grounding via shared.grounding.is_grounded | a plain substring check matches "1" inside "2019"
    """
    if not x or not series or any(len(v) != len(x) for v in series.values()):
        return None
    for values in series.values():
        for v in values:
            if not is_grounded(v, corpus):
                return None
    return {"x": list(x), **{name: list(v) for name, v in series.items()}}


def _extract_numeric_data_from_draft(draft: str, chart_title: str) -> dict[str, Any] | None:
    """Try to find a markdown table in the draft that matches the chart title."""
    import re

    # Look for a table near the chart title mention
    title_pattern = re.compile(re.escape(chart_title), re.IGNORECASE)
    lines = draft.splitlines()
    for i, line in enumerate(lines):
        if title_pattern.search(line):
            # Look for a markdown table in the next 20 lines
            table_lines = [
                l for l in lines[i : i + 20] if "|" in l and not l.strip().startswith("|---")
            ]
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
    except Exception as exc:
        logger.error({"event": "fetch_chart_data_error", "error_type": type(exc).__name__})
    return None


def export_documents(state: DraftingState) -> dict[str, Any]:
    """Present the draft; exports require a separate authenticated review action.

    Agent state and payload fields cannot prove a user reviewed the completed
    response. Do not render files or publish export records from this node.
    """
    document: ResponseDocument | None = state.get("draft_document")
    draft_text = state.get("draft", "")
    chart_specs_raw: list[dict] = state.get("chart_specs", [])
    doc_type = state.get("document_type", "document")
    doc_type_str = doc_type.value if hasattr(doc_type, "value") else str(doc_type)

    if document is not None:
        title = document.title
        sections: list[dict] = document_sections_for_export(document)
    else:
        # Derive title from first heading in draft or fallback
        import re

        title_match = re.search(r"^#\s+(.+)$", draft_text, re.MULTILINE)
        title = title_match.group(1).strip() if title_match else f"{doc_type_str}_document"

        # Build sections from draft headings
        sections = []
        current_heading = ""
        current_content_lines: list[str] = []
        order = 0
        for line in draft_text.splitlines():
            if line.startswith("## "):
                if current_content_lines:
                    sections.append(
                        {
                            "heading": current_heading,
                            "content": "\n".join(current_content_lines).strip(),
                            "order": order,
                        }
                    )
                    order += 1
                    current_content_lines = []
                current_heading = line[3:].strip()
            elif line.startswith("# "):
                pass  # Skip document title line
            else:
                current_content_lines.append(line)
        if current_content_lines:
            sections.append(
                {
                    "heading": current_heading,
                    "content": "\n".join(current_content_lines).strip(),
                    "order": order,
                }
            )

    chart_sections = {sd["title"]: sd.get("section", "") for sd in chart_specs_raw if isinstance(sd.get("title"), str)}
    if document is not None:
        final_text = render_document_markdown(document, chart_mode="table")
    else:
        final_text = _render_sections_text(title, sections, list(chart_sections), chart_sections)

    return {
        "messages": [AIMessage(content=final_text)],
        "exports": [],
        "trace": [],
    }
