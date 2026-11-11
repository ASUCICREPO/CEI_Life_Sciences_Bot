# F14 — Analysis agent
Status: partial (MVP slice, B94: Analysis runtime in CDK, scope pause and resume; `InMemorySaver`; Web Search confirmed for Analysis under the same off-by-default flag as Research (B108); full phase pending) | Depends on: F6, F13 | Decisions: B26, B30, B51, B72, B85, B108 | Specs: ../specs/agents.md, ../specs/tools.md

## Goal
Implement the Analysis Agent's real logic: gather evidence, analyze it, build comparison matrices, and produce a mode-aware competitor/market/legal-landscape report with citations, presented as CEI UI.

## Scope — In / Out

**In**
- Full implementation of `gather`, `analyze`, `compare`, `report` in `backend/agents/analysis/nodes.py` (today all four `raise NotImplementedError`).
- A scope-confirmation HITL point before `gather`: competitors, market, and jurisdiction are confirmed with the user before the analysis runs (B72's explicit requirement for Analysis), reusing F13's `interrupt`-based mechanism.
- The same tool access as Research/Drafting: all 10 data source Lambdas plus NCBI (B26, B30, B85), LS Ops, Web Search, Code Interpreter.
- Science/business mode variants via `PromptLoader`, matching Research/Drafting's existing pattern.
- A CEI UI presenter for the Analysis output (comparison matrix + report), following the existing `build_research_surface`/`build_drafting_surface` pattern.

**Out**
- Async/background execution for long analysis runs (F16 delivers the mechanism; this phase's entrypoint stays synchronous like Research's current `main.py`, to be converted once F16 exists).
- Gateway-mediated tool calls (F4); until F4 ships, Analysis calls `DataSourceTool.invoke()` directly in `direct_mode`, exactly like Research and Drafting do today.
- Long-term memory wiring (F11 delivers the helper; this phase calls it the same way Research/Drafting do, as a small addition once F11 ships — listed as a task below, not a blocker).

## Deliverables
- `backend/agents/analysis/state.py` — exists (`AnalysisState`, `AnalysisType`, `Comparison`). Add: `confirmed_scope: bool`, `competitors: list[str]`, `market: str | None`, `jurisdiction: str | None` — the fields the scope-confirmation HITL point fills in before `gather` runs.
- `backend/agents/analysis/nodes.py` — implement all four existing stub functions, plus a new `confirm_scope` function.
- `backend/agents/analysis/graph.py` — exists (`gather → analyze → compare → report → END`, no checkpointer wiring beyond the parameter). Add `confirm_scope` as the entry node, before `gather`.
- `backend/agents/shared/presenters/cei_ui.py` (or a new `shared/presenters/analysis.py` alongside the existing presenter module — match whichever the F6 streaming-UI phase settles on) — add `build_analysis_surface(text, surface_id, comparisons, document)`.
- `backend/agents/analysis_main.py` (new) — an AgentCore Runtime entry point mirroring `backend/agents/main.py`'s structure (checkpointer setup, thread eviction, `invoke` entrypoint) but calling `analysis.graph.compile_graph()`.
- `backend/agents/shared/prompts/` — add or extend the Analysis system prompt (science/business variants), following the same file layout Research/Drafting already use.

## Tasks
- [ ] 1. Add `confirm_scope` node: build a `ReviewGate`/`ChoicePicker`-style HITL surface asking the user to confirm competitors, market, and jurisdiction; call it via F13's `interrupt` mechanism.
- [ ] 2. Wire `confirm_scope` as the new entry point in `analysis/graph.py`; route to `gather` after confirmation.
- [ ] 3. Implement `gather`: call the same `DataSourceTool` set Research uses, selecting sources relevant to `analysis_type` (competitor/market/legal_landscape).
- [ ] 4. Implement `analyze`: turn gathered `DataSourceResult`s into structured findings.
- [ ] 5. Implement `compare`: build `Comparison` objects (dimension, findings, sources) — **Proposal (not decided):** whether this uses a fixed framework (for example SWOT) per `analysis_type`, or an LLM-chosen framework each time; agents.md does not decide this, so start with a fixed framework per type and revisit if it does not fit real inputs.
- [ ] 6. Implement `report`: produce the final `ResponseDocument`, grounded via `shared/grounding.py` (reuse, do not reimplement).
- [ ] 7. Add `build_analysis_surface` presenter.
- [ ] 8. Add `analysis_main.py` entry point, following `main.py`'s structure exactly (checkpointer, thread eviction, `_format_trace`, `invoke`).
- [ ] 9. Add Analysis prompt files (science/business) under `shared/prompts/`.
- [ ] 10. Once F11 ships, add the `fetch_long_term_context` call to `analysis_main.py`'s `invoke`, matching Research/Drafting.

## Acceptance criteria
- A competitor-analysis prompt with an ambiguous market pauses for scope confirmation before any data source is called.
- After scope confirmation, the graph produces a `ResponseDocument` with at least one `Comparison` and a grounded report, rendered through `build_analysis_surface`.
- Business mode and science mode produce different prompt framing for the same input (verified by prompt-loader unit test, not by asserting on LLM output).
- The graph never calls a data source before `confirm_scope` completes.

## Tests
- Unit (pytest): `confirm_scope` interrupt payload shape; `gather`/`analyze`/`compare`/`report` with mocked `DataSourceTool.invoke()` results.
- Unit: `build_analysis_surface` presenter — valid CEI UI 1.1 output, matches the allowlisted component graph.
- Unit: graph compiles and routes `confirm_scope → gather → analyze → compare → report → END` in order.
- Integration (local, direct mode): a full local run against the existing local data source classes, verifying end-to-end structured output.

## Risks and open items
- **Resolved:** the decision-log Phase list and this roadmap now list F13 as an explicit dependency of this phase (not just a practical one), matching this phase's real need for F13's `interrupt` mechanism before `confirm_scope` can pause the graph.
- Comparison framework design (`compare` node) is a **Proposal (not decided)** — agents.md does not specify one; flagged above.
- HITL point design for Analysis was itself a "Proposal (not decided)" in `agents.md` §3 before B72; B72 has now decided the *point* (scope confirmation before analysis) but not its exact UI shape (multiple-choice vs. free text vs. a mixed ReviewGate) — this phase picks a `ReviewGate`-with-editable-fields shape as a reasonable default, not a decided one.
- Decided fix, not yet wired: the scope-correction text (`state["scope_correction"]`, applied in `scope()` via `scope.j2`) goes into the `scope.j2` system prompt today, not into the guarded `user_turn()` content that `shared/runtime/guarded_input.py` checks for. The decided fix is to move it into the guarded user turn, matching every other user-typed-text call site in this agent (B51a's input-masking scope). `nodes.py`'s `scope()` still needs this change.

## MVP slice (B94, B96, B97)
Built before F6/F13 for the client demo, local only. In scope: all three analysis types with a fixed framework per type, graph `scope → confirm_scope → gather → analyze → compare → report`, the scope gate as a real `interrupt()` on the in-process `InMemorySaver` (resumed by `op: "resume"`), the new openFDA and PatentsView direct-mode sources, `build_analysis_surface` (CEI UI 1.1), `analysis_main.py` on port 8083, and `analysis/run.py` `run_analysis(...)` for the Pipeline Manager. Still left for the full phase: F6 streaming, F13's durable checkpointer and `/ui-actions`, F4 Gateway calls, F11 memory.

## Handoff notes
- F17 (Pipeline Manager) will call the Analysis Agent as one of its dispatched sub-tasks — it needs `analysis_main.py`'s entrypoint contract (`{prompt, mode, user_id, session_id, analysis_type}` in, `ResponseDocument` + CEI UI out) to be stable before it can wire dispatch.
- F16 (background jobs) will convert `analysis_main.py`'s synchronous entrypoint to the async-session pattern; keep the node logic itself free of any assumption about sync vs. async execution.
