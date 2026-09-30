# Agent Topology

Status: planned. Source of truth: `.agents/decision-log.md`. See `architecture.md` for the request flows and `tools.md` for tool detail. Model, streaming, and action-endpoint wire formats belong to `api.md` and `streaming-protocol.md` (sibling specs) — referenced here by name only.

All four specialist agents plus the tool-agent runtime are Python LangGraph applications on Bedrock AgentCore Runtime (B3). Current code lives under `backend/agents/`.

## 1. Research Agent (Goal 1)

**Purpose:** Deep research across the 10 data sources with citation grounding (D1, B26).

**Graph nodes** (`backend/agents/research/graph.py`, `research/nodes.py` — exists):
`triage → retrieve → synthesize → cite → END`

| Node | Behavior today |
|---|---|
| `triage` | Structured-output call picks sources + per-source queries (`TriagePlan`). |
| `retrieve` | Calls each selected `DataSourceTool.invoke()` (direct HTTP today; Gateway MCP call in target). Falls back to PubMed if triage selects nothing. |
| `synthesize` | Structured `ResponseDocument` call, grounded by `shared/grounding.py`; falls back to prose on parse/render failure. |
| `cite` | Resolves `[SourceID]` markers into a bibliography from tool-returned citations. |

**Inputs/outputs:** `{prompt, mode, user_id, session_id}` → `ResponseDocument` (`shared/schemas/response_blocks.py`) → `build_research_surface` presenter → CEI UI `ui` field + plain `text` (exists — see `docs/cei-ui-protocol.md`).

**Tools/scopes:** all 11 data source Lambdas — the 10 sources plus NCBI (specialist agents get every source scope, including NCBI — B30/B85), Code Interpreter, Life Sciences Operations Lambda (B42), Web Search connector (B44), KB retrieval tool for chat attachments (B33/B35).

**Mode variants:** science/business system prompt via `PromptLoader` (`shared/prompts/loader.py`) — exists.

**HITL points (B72):** a clarifying question when `triage` finds no suitable source or the request is ambiguous, in place of the current silent PubMed fallback. Research can also ask the user multiple-choice or long free-text questions at any point via `ask_user_question`/`get_user_input`. Not wired into the graph today (`shared/tools/hitl/inline_functions.py` exist generically; no node calls them) — gap, planned F13.

**Checkpointer:** target `AgentCoreMemorySaver` (B28). Current: `InMemorySaver` in `main.py`, with a hand-rolled LRU eviction (`_thread_ids`, limit 128) — gap.

**Sync vs. async:** target — sync over SSE normally; async when the triage step's estimate exceeds 10 minutes (B23/B79), decided once from the triage estimate before any streaming starts (no mid-run handoff). Current: always synchronous, local `graph.ainvoke()`, no estimator — gap.

**Async entrypoint pattern (target, B25):** entrypoint returns immediately; work runs under `add_async_task`/`complete_async_task`; runtime pings `HealthyBusy` while busy; cancel is a cooperative flag checked between nodes plus `StopRuntimeSession`. Not implemented.

**Token refresh (B67):** tool tokens live 1 h. A running agent (real-time or async) calls the internal token-refresh endpoint (IAM auth, `POST /internal/token-refresh` — see `api.md`, `auth-and-tool-scope.md`) before its token expires. The endpoint checks the Job ID Store (background) or the active-run record (real-time) and returns a new token with the same scopes and user; the agent never holds the M2M client secret. Not implemented.

**Prompt caching (D16):** not implemented. `get_model()` (`shared/utils.py`) builds `ChatBedrockConverse` with no `cachePoint` on the system prompt or tool defs — gap, planned F8.

**Guardrails (B51/B51a):** not configured on any model call — gap, planned F8.

**Model selection + blocklist (B18):** `shared/config.py` has one fixed `default_model_id`; no per-chat picker, no blocklist enforcement — gap, planned F8.

**Router classifier contract:** Research is a routing *target*, not the classifier. See §5.

**Async prediction rule (B23/B79):** not implemented; no plan-step estimator exists. Target threshold: 10 minutes.

## 2. Drafting Agent (Goal 2)

**Purpose:** Document preparation (grants, VCs, journals, whitepapers, LaTeX) — D1.

**Graph nodes** (`drafting/graph.py`, `drafting/nodes.py` — exists):
`research_guidelines → plan_research →(cond)→ [research_subagent →] draft →(cond)→ [revise →(cond) loop] → plan_visuals → export_documents → END`

| Node | Behavior today |
|---|---|
| `research_guidelines` | Two structured calls: query plan per source, then guideline extraction from retrieved evidence. |
| `plan_research` | Decides if the doc needs Research-Agent evidence; writes up to 3 questions. |
| `research_subagent` | Runs the **Research Agent graph in-process**, batched, no checkpointer (stateless per drafting run) — this is today's only cross-agent call. Target (B71): Drafting keeps this research subgraph in-process (same runtime and session), using the Gateway tools directly; it does not call the Research runtime through the Session Router. |
| `draft` / `revise` | Structured `ResponseDocument` calls, grounded; falls back to prose. `revise` loops based on `revision_feedback`. |
| `plan_visuals` | Chart specs from the document's chart blocks, or an LLM chart-detection pass with number-boundary grounding. |
| `export_documents` | Renders charts via Code Interpreter, exports PDF/DOCX/MD/CSV/LaTeX/TXT (`shared/tools/export*.py`). Agent-produced during a run; a completed answer also carries a user-triggered Export action (`POST /sessions/{id}/turns/{turn}/export`, B74) that any agent's answer can use, not only Drafting's own export node. |

**Inputs/outputs:** `{prompt, mode, document_type, export_formats}` → `ResponseDocument` → `build_drafting_surface` + `exports[]` (format/filename/byte_size) — exists.

**Tools/scopes:** same as Research, plus Chart tool and Export tool (Code Interpreter-backed).

**Mode variants:** science/business via `PromptLoader`, same mechanism as Research.

**HITL points (B72):** outline approval before the full draft, and revision requests through a LangGraph `interrupt` that resumes into the revise loop. `revision_feedback` is a plain state field set externally today, not an `interrupt` — gap, planned F13.

**Checkpointer / sync-async / caching / guardrails / model selection:** same gaps as Research (§1). The in-process `research_subagent` call is resolved by B71 (kept in-process, using the Gateway tools of the Drafting session) and by B70 (the shared `cei-tools/common` scope plus Drafting's own full source-scope grant — B30 — already covers everything the in-process research subgraph needs; no separate Router-mediated call or broader scope grant is required).

**Token refresh (B67):** same mechanism as Research §1.

**Async prediction rule (B23):** Drafting is not Pipeline Manager, so it follows the plan-step-estimate rule, not the "always async" rule.

## 3. Analysis Agent (Goal 3)

**Purpose:** Competitor/market/legal landscape analysis, mode-aware output (D1, D4).

**Graph nodes** (`analysis/graph.py`, `analysis/nodes.py` — wired but unimplemented):
`gather → analyze → compare → report → END`. All four node functions currently `raise NotImplementedError` — this agent has no working logic yet.

**Target inputs/outputs, tools, mode variants:** same pattern as Research/Drafting — `ResponseDocument` → presenter → CEI UI; all 11 sources (10 + NCBI, B85) + LS Ops + Web Search + Code Interpreter.

**HITL points (B72):** scope confirmation (competitors, market, jurisdiction) before the analysis runs. Not implemented — graph body doesn't exist yet.

**Gap:** entire agent body (F14).

## 4. Pipeline Manager (Goal 4)

**Purpose:** Orchestrates Research/Drafting/Analysis in concurrent, sequential, or looped tasks; plan approval; self-healing (D1, D10, B54).

**Graph nodes** (`pipeline_manager/graph.py`, `pipeline_manager/nodes.py` — wired but unimplemented):
`plan → approve →(cond: plan_approved)→ [dispatch ⇄ monitor loop, routed by should_continue] → synthesize → END`. All five node functions `raise NotImplementedError`. `should_continue` (the only implemented piece) inspects step statuses and routes to `monitor`/`dispatch`/`synthesize`, retrying failed steps up to 3 times.

**HITL points (B72):** `approve` calls `request_plan_approval` (exists as a generic inline tool) and uses a LangGraph `interrupt` per B56 — not implemented. Per the general B72 clause, Pipeline Manager can also ask the user multiple-choice or long free-text questions at any point.

**Sync vs. async:** **always async** per B23 — Pipeline Manager runs are never real-time SSE.

**Dispatch mechanism (B83):** each `dispatch` step calls the target specialist agent's runtime with `InvokeAgentRuntime` (not an in-process call, unlike Drafting's own research subgraph — B71). Concurrent steps run in separate AgentCore Runtime sessions, each with its own 2 vCPU / 8 GB allocation, and each step gets its own trace (visible independently in the trace log, not merged into one Pipeline Manager trace). Not implemented.

**Async entrypoint / checkpointer / caching / guardrails / model selection:** same target mechanism as §1, entirely unbuilt for this agent (F17).

**Token refresh (B67):** same mechanism as Research §1 — required, since Pipeline Manager runs are always async and can exceed the 1 h token TTL.

**Gap:** entire agent body (F17), depends on F13 (HITL) and F16 (background jobs).

## 5. Tool Agent Runtime (single-source chat)

**Purpose:** A natural-language layer over exactly one data source at a time (B21). Matches the founder-ai-dashboard `ToolsPage` UX ("Ask PubMed…", NCBI quick tools). Reached over its own streaming route, `POST /tools/{source}/chat/stream` (B84), not `/chat/stream` — same Session Runner Lambda, same event catalog; the Router reads `source` from the path to pick the profile and the single-source token to request.

**Proposed shape (B29, not yet built):** 1 AgentCore Runtime, 1 generic LangGraph graph, 11 YAML profiles (10 sources + NCBI). Each request loads the profile for the requested source and the agent gets a Gateway token scoped to only that source (B30). Standard tools (Code Interpreter) remain available; no other external data source is reachable.

**Optional hooks (per decision-log wording):** a custom subgraph or presenter hook per profile, for sources that need source-specific interaction (e.g., NCBI BLAST submit/poll is asynchronous within the tool-agent turn). **Proposal (not decided):** exact hook interface (e.g., a Python entry point per YAML profile vs. a fixed node set with per-profile config only).

**Inputs/outputs:** natural-language query for one source → `ResponseDocument`-style answer for that source, presented as CEI UI (reuses the same presenter contract as the specialist agents where possible — TBD in `api.md`).

**Checkpointer:** `AgentCoreMemorySaver`, scoped per tool-agent session; no cross-source memory.

**Gap:** 100% planned (F15). No code exists under `backend/agents/` for this runtime today.

## 6. Cross-Agent Mechanisms (target, mostly ungapped-vs-built)

**Prompt caching (D16):** cache checkpoints on the static system prompt and tool definitions; conversation history stays uncached. Applies to all five runtimes above. Not implemented anywhere in `shared/utils.get_model()` today.

**Guardrails (B51/B51a):** Bedrock Guardrails on every agent model call; PII/PHI filter masks user input only (tool results/model output keep public NPI/author data). Guardrail config is never shown in the trace (B7). Not implemented.

**Model selection + blocklist (B18/D3):** user selects any Bedrock model per chat; a configurable env-var blocklist removes specific model IDs app-wide. `get_model(model_id=None)` already accepts an override parameter, but nothing in the current call sites passes a user-selected `model_id`, and there is no blocklist check — partial gap, mechanism exists, wiring does not.

**Router classifier contract (B15/B17):** Session Router Lambda. Dashboard task launcher requests use a deterministic lookup (task type → agent, no model call). Free-text chat calls a small, fast, **fixed** Bedrock model (independent of the user's chat model choice) with structured output `{agent: "research"|"drafting"|"analysis"|"pipeline_manager", confidence: float}`. **Low-confidence fallback (B80):** when the classifier's confidence is below its threshold (an implementation default, tuned in F9 — not a decision-log value), the Router does not guess; it returns a `ChoicePicker` surface ("Which kind of help do you want?") listing the 4 agents, and the user's pick is what starts the run. Not implemented — the Session Router Lambda does not exist yet.

**Async prediction rule (B23/B79):** Pipeline Manager runs are always async. For the other three agents, a plan/triage step estimates step count and source count; if the estimate exceeds 10 minutes, the run goes async instead of SSE (B79). This is a one-time decision made before streaming starts — there is no mid-run handoff from a streaming run to a background job.

**Token refresh (B67):** all five runtimes call the internal token-refresh endpoint before their tool token's 1 h TTL expires; see §1. Not implemented.

**HITL points, general clause (B72):** all agents can ask the user questions (multiple choice and long free-text answers), in addition to the agent-specific HITL points listed in §1–§4. None of this is wired to a LangGraph `interrupt` yet (F13, S4).

## 7. Current Code vs. Target — Gap Table

| Area | Current | Target | Phase |
|---|---|---|---|
| Research graph | Implemented, runs locally via CLI | Same graph, deployed to AgentCore Runtime, Gateway-backed tools | F5 |
| Drafting graph | Implemented, including in-process Research sub-call | Same graph, deployed; in-process cross-agent call pattern is decided (kept in-process, B71) | F7 |
| Analysis graph | Skeleton only, all nodes `NotImplementedError` | Full implementation | F14 |
| Pipeline Manager graph | Skeleton only, all nodes `NotImplementedError` | Full implementation + real plan approval + self-healing | F17 |
| Tool Agent runtime | Does not exist | 1 runtime, 1 graph, 11 profiles | F15 |
| Checkpointer | `InMemorySaver` (all 4 agents) | `AgentCoreMemorySaver` everywhere | S3/S4, then F5+ |
| HITL | Generic tools exist; not wired into any graph as `interrupt` | Real pause/resume via `interrupt`, typed POST action resume | F13, S4 |
| Data source calls | Direct HTTP (`_direct_invoke`) | AgentCore Gateway MCP call, Cedar-scoped | F4 |
| Model selection | One fixed `default_model_id` | Per-chat user-selected model + env-var blocklist | F8 |
| Prompt caching | None | `cachePoint` on system prompt + tool defs | F8 |
| Guardrails | None | Bedrock Guardrails, input-only PII/PHI mask | F8 |
| Async execution | None (always sync CLI run) | `add_async_task`/`complete_async_task`, `HealthyBusy`, cooperative cancel | F16, S3 |
| Router | Direct Python function call to `compile_graph()` | Session Router Lambda: deterministic + classifier | F5, F9 |

## 8. TBD

- Tool Agent per-profile custom hook interface — Proposal (not decided).
- AgentCore Memory short-term event expiry: the maximum value is decided in principle (the maximum available expiry, expected around 365 days — B82) but the exact number still needs verification against AWS documentation; the HITL behavior once it expires is decided ("expired, start again" — B82).
- Router low-confidence threshold value — implementation default, tuned in F9 (B80).

Resolved by B67–B86: token refresh mechanism (B67), Drafting's in-process research subgraph (B71), HITL points for Research/Drafting/Analysis/Pipeline Manager plus the general ask-a-question clause (B72), export triggers (B74), no per-user model restriction (B76), async threshold is 10 minutes with no mid-run handoff (B79), Router low-confidence fallback is a `ChoicePicker` of the 4 agents (B80), Pipeline Manager dispatches specialist steps via `InvokeAgentRuntime` in separate sessions/traces (B83), the tool-agent stream route (B84), specialist agents also get the NCBI scope (B85).
