# F15 — Tool agents
Status: not started | Depends on: F4, F6 | Decisions: B21, B27, B29, B30, B69, B84, B85, B86 | Specs: ../specs/agents.md, ../specs/tools.md, ../specs/auth-and-tool-scope.md, ../specs/api.md (`/tools/{source}/chat/stream`)

Implementation note: the taxonomy and sequence Gateway targets and their shared
PubMed/NCBI E-utilities rate limiter are implemented, but have not been deployed
or validated through a live Gateway. BLAST is not exposed: associating a request
identifier with its owner requires the B99 trusted-identity proof. The NCBI tool
agent can bind the two read-only tools, but cannot yet complete BLAST. The
phase remains incomplete; the generic runtime, route, and end-to-end isolation
still require integration and proof.

## Goal
Build the single-source, natural-language "tool agent": one AgentCore Runtime, one generic LangGraph graph, 11 YAML profiles (the 10 existing sources + NCBI), so a user can chat with exactly one data source at a time — matching the founder-ai-dashboard `ToolsPage` UX ("Ask PubMed…", NCBI quick tools).

## Scope — In / Out

**In**
- One generic tool-agent graph, parameterized per-request by a YAML profile naming exactly one source scope (B29).
- 11 YAML profiles: `pubmed`, `clinicaltrials`, `biorxiv`, `medrxiv`, `chembl`, `opentargets`, `cmscoverage`, `icd10`, `npi`, `pubchem`, `ncbi` (B27).
- Three new NCBI Gateway targets: taxonomy lineage, sequence/efetch, BLAST submit/poll (B27, `tools.md` §2).
- Cedar enforcement of exactly one source scope per tool-agent request (B30, B69 — one Cedar permit per source target).
- The tool-agent streaming route: `/tools/{source}/chat/stream` (STREAM mode, same Session Runner as `/chat/stream`), per B84. The Router reads the `source` path parameter and issues a token scoped to only that one source.
- Code Interpreter and the shared `cei-tools/common` scope (LS Ops, Web Search — from F12) remain available to the tool agent; no other external data source is reachable in a given request.
- `AgentCoreMemorySaver` checkpointer, scoped per tool-agent session (no cross-source memory).

**Out**
- Specialist-agent access to tool agents — B26 says specialist agents never call tool agents; both paths share the Gateway Lambdas, but this phase does not touch the specialist agents.
- Long-term memory for the tool agent (flagged as a **Proposal (not decided)** in F11; not resolved here).
- Any custom per-source subgraph beyond what BLAST's submit/poll needs (see Tasks/Risks — the exact hook interface is undecided).

## Deliverables
- `backend/agents/tool_agent/` (new package): `graph.py`, `nodes.py`, `state.py`, `profile.py` (YAML loader).
- `backend/agents/tool_agent/profiles/pubmed.yaml`, `clinicaltrials.yaml`, `biorxiv.yaml`, `medrxiv.yaml`, `chembl.yaml`, `opentargets.yaml`, `cmscoverage.yaml`, `icd10.yaml`, `npi.yaml`, `pubchem.yaml`, `ncbi.yaml` (new) — each names one source scope, one tool binding, and a short profile-specific system-prompt fragment.
- `backend/agents/tool_agent/main.py` (new) — AgentCore Runtime entrypoint, following `backend/agents/main.py`'s structure, taking a `source_id` field to select the profile.
- `backend/agents/shared/tools/data_sources/ncbi_taxonomy.py`, `ncbi_sequence.py`, `ncbi_blast.py` (new) — `DataSourceTool` subclasses, following the existing pattern in `shared/tools/data_sources/pubmed.py` etc., each with a working `_direct_invoke()` for local/CLI testing.
- `backend/lib/backend-stack.ts` — add the Tool Agent AgentCore Runtime construct, the three new NCBI Gateway targets, the `cei-tools/src.ncbi` scope wiring (coordinate with F4's Cedar policy file), and the `/tools/{source}/chat/stream` STREAM route (B84) on the existing `RestApi`, reusing the same Session Runner Lambda F5 built.
- `backend/lambdas/session-router/` (modify, from F5/F9) — accept the `source` path parameter from `/tools/{source}/chat/stream`, validate it against the 11 known source IDs, and request an M2M token scoped to exactly that one source (B84).

## Tasks
- [ ] 1. Design the YAML profile schema (source scope name, MCP tool name(s) from `tools.md`, system-prompt fragment, optional custom-hook reference).
- [ ] 2. Write the 11 profile files.
- [ ] 3. Build the generic tool-agent graph: `load_profile → query → respond → END` (or similar minimal shape — no triage step needed, since the source is already chosen).
- [ ] 4. Build `ncbi_taxonomy.py` (`ncbi-taxonomy___lineage`), `ncbi_sequence.py` (`ncbi-sequence___efetch`), `ncbi_blast.py` (`ncbi-blast___submit` / `ncbi-blast___poll`) as `DataSourceTool` subclasses.
- [ ] 5. Design and implement the BLAST submit/poll interaction inside the tool-agent graph as a wait loop (poll interval, see Risks) — this phase resolves the previously open Proposal in `tools.md`/`agents.md` in favor of a generic async-tool pattern in the shared graph, not a per-profile custom subgraph hook, unless BLAST proves this insufficient during implementation.
- [ ] 6. Add the Tool Agent AgentCore Runtime construct in `backend-stack.ts`.
- [ ] 7. Add the three NCBI Gateway targets and the `cei-tools/src.ncbi` scope (verify against the substring-collision rule in `auth-and-tool-scope.md` §3 — `ncbi` is already checked clean against the other 10 scope names). Once this scope exists, update every specialist-agent token request (F5's Router, and F14/F17 once they exist) to also include `cei-tools/src.ncbi`, per B85 — specialist agents get NCBI access too, not just tool agents.
- [ ] 7a. Add the `/tools/{source}/chat/stream` STREAM route (B84) to the existing `RestApi`, reusing F5's Session Runner Lambda and Cognito authorizer; extend the Session Router to read `source` from the path and request a single-source-scoped token.
- [ ] 8. Reuse a presenter (either `build_research_surface` if its shape fits a single-source answer, or a new minimal `build_tool_agent_surface`) to render the tool agent's answer as CEI UI.
- [ ] 9. Add `tool_agent/main.py` entrypoint, following `main.py`'s checkpointer/thread-eviction pattern.

## Acceptance criteria
- A request naming `source_id: "pubmed"` only ever calls the PubMed tool, never any other source, verified by a test that asserts the loaded profile's tool list has exactly one entry.
- A request naming `source_id: "ncbi"` can complete a BLAST submit-then-poll cycle within one tool-agent turn.
- The tool agent's Gateway token (once F4/this phase's Cedar wiring is live) carries exactly one source scope, matching `auth-and-tool-scope.md` §7 ("Tool agent (per profile): exactly one source scope").
- Removing or renaming a profile file does not affect any other profile (profiles are fully independent).
- `POST /tools/pubmed/chat/stream` streams a real, single-source answer over SSE using the same Session Runner as `/chat/stream`, and the Router-issued token carries only the `pubmed` scope (B84).

## Tests
- Unit (pytest): YAML profile loader — valid profile, missing profile, malformed profile.
- CDK assertion: `/tools/{source}/chat/stream` route exists with `responseTransferMode: STREAM` and the same Cognito authorizer as every other route.
- Unit: each of the three new NCBI `DataSourceTool` subclasses' `_direct_invoke()` against a mocked HTTP response.
- Unit: the generic tool-agent graph, run once per profile, with a mocked tool response, confirming only that profile's tool is ever invoked.
- Unit: BLAST submit/poll loop with a mocked "still running" then "done" poll sequence.
- CDK assertion tests: Tool Agent Runtime construct exists; the three NCBI Gateway targets exist; cdk-nag passes or carries an ADR-format suppression.
- Integration: end-to-end local run for at least 2 of the 11 profiles (one simple, one BLAST) using direct-mode `DataSourceTool`s before the Gateway exists.

## Risks and open items
- **Proposal (not decided), resolved as a working default by this phase:** BLAST's async submit/poll uses a generic wait loop in the shared tool-agent graph rather than a per-profile custom subgraph hook. If a future source needs something the generic pattern cannot express, revisit — do not silently special-case it without updating this file and `tools.md`.
- BLAST QBLAST poll interval and concurrent-submission limits are still **TBD** (`tools.md` §9) — this phase must pick a conservative default (for example, poll every 60s per the NCBI courtesy guideline cited in `tools.md`) and treat it as configurable, not hardcoded, until the real limit is confirmed.
- Upstream rate limits for the other 9 non-NCBI sources are already listed as TBD in `tools.md` — this phase inherits whatever retry/backoff F4 built for the specialist-agent path; it does not need to re-derive them, only reuse them.
- `tools/list` filtering by scope tag (so a tool agent with only one scope never sees the other tools) is an explicit open prototype item from B30/S2 — this phase depends on F4 having already proven this works; if S2/F4 have not verified it, tool-agent isolation is unverified, not just unimplemented.

## Handoff notes
- The founder-ai-dashboard `ToolsPage.tsx` (ported to `frontend/` per B1/B66, in F2) already has the UX shape this phase's API needs to match: a source picker, a natural-language query box, and an NCBI quick-tools section. When the frontend port lands, wire `ToolsPage.tsx`'s mocked run flow (`src/mocks/tools.ts`, `src/mocks/ncbiTools.ts`) to the real `/tools/{source}/chat/stream` route (B84) this phase adds — the route shape is decided (a separate route, not a `source_id` field on `/chat/stream`), resolving the earlier open question.
- F5's Session Router (and F14/F17 once they exist) must add `cei-tools/src.ncbi` to every specialist-agent token request once this phase's NCBI scope exists (B85).
