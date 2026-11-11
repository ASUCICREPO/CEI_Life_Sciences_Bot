# F07 — Drafting + export

Status: partial (Drafting runtime in CDK; export service with review-acknowledgment check; Exports bucket, ExportMetadataStore, list and download routes; outline approval, ad hoc export route and deployed checks pending) | Depends on: F6 | Decisions: B41, B61, B71, B74, B78, B81 | Specs: [`../specs/agents.md`](../specs/agents.md) §2 (Drafting Agent), [`../specs/tools.md`](../specs/tools.md) §8 (Export Tools), [`../specs/api.md`](../specs/api.md) (`/exports`), [`../specs/data-model.md`](../specs/data-model.md) §7 (Export outputs bucket)

## Goal

Deploy the existing Drafting agent graph to AgentCore Runtime (same pattern as F5's Research deployment) and make document export (PDF, DOCX, MD, CSV, LaTeX, TXT) work in production: real Code Interpreter-backed rendering, a real S3 export outputs bucket with presigned downloads, the review-acknowledgment gate before export, and both export triggers from B74 (agent-produced during a run, and a client-initiated `POST` export of a completed answer).

## Scope — In / Out

**In:** the Drafting AgentCore Runtime deployment, `AgentCoreMemorySaver` wiring (same as F5), the Export outputs S3 bucket + presigned GET (15 min TTL per B78), the `GET /exports/{exportId}/download` route, the client-initiated export route implied by B74 (`POST /sessions/{id}/turns/{turn}/export`), granting the Code Interpreter to the Drafting runtime (`codeInterpreter.grantUse`, the TODO already flagged in `backend-stack.ts`), and the review-acknowledgment gate (D11, shaped by B81) before any export completes.

**Out:** Drafting's `revise` loop becoming a real `interrupt` (B72/F13 decide and build this — this phase keeps `revision_feedback` as a plain state field, unchanged from today, unless F13 has already landed and changed the pattern, in which case follow F13's actual implementation, not this phase's placeholder), Slides/PPTX export (deferred, B41), the in-process Research sub-call's Gateway-token scope question (B71 decides the in-process pattern stays; this phase does not need to change `research_subagent`'s call shape, only confirm it still works against F4/F5's live Gateway-backed Research tools).

## Deliverables

- `backend/agents/drafting/graph.py`, `drafting/nodes.py` (modify) — no structural change to `research_guidelines → plan_research →(cond)→ [research_subagent →] draft →(cond)→ [revise →(cond) loop] → plan_visuals → export_documents → END`; confirm `research_subagent`'s in-process call to the Research graph works against F4/F5's live Gateway tools (B71: kept in-process, not routed through the Session Router).
- `backend/agents/drafting_main.py` (modify) — same `AgentCoreMemorySaver` wiring as F5's `main.py`.
- `backend/lib/backend-stack.ts` (modify) — add the Drafting `AgentCoreRuntime` construct, grant Code Interpreter use to it (`codeInterpreter.grantUse(draftingRuntimeRole)`, resolving the existing TODO comment), add the Export outputs S3 bucket (private, presigned GET only).
- `backend/lambdas/session-router/` (modify, from F5) — add the Drafting entry to the deterministic task→agent lookup table (Goal 2 dashboard tasks).
- `backend/agents/shared/tools/export.py`, `export_pdf.py`, `export_docx.py` (modify) — switch `output_dir`-based local file writes to the Export outputs S3 bucket; add the review-acknowledgment gate check before `export_documents` proceeds (currently missing entirely, per `tools.md` §8's named gap). Per B81, the gate is an export dialog with an "I have reviewed this output" checkbox; the Export action stays disabled until the checkbox is checked, and the acknowledgment (user, turn, timestamp) is logged.
- `backend/lambdas/exports-handler/` (new, Python) — `GET /exports/{exportId}/download` (presigned GET, 15 min TTL per B78) and the client-initiated `POST /sessions/{sessionId}/turns/{turnId}/export` route (B74).
- `backend/lib/backend-stack.ts` (modify) — add both export routes to the existing `RestApi` (BUFFERED).

## Tasks

- [ ] 1. Deploy the Drafting graph to an AgentCore Runtime, following F5's exact pattern (CodeZip asset, `AgentCoreMemorySaver`, Gateway-scoped token via the Router).
- [ ] 2. Add the Drafting entry to the Session Router's deterministic lookup table.
- [ ] 3. Grant Code Interpreter use to the Drafting runtime's execution role in `backend-stack.ts`, resolving the existing `// When ... grantUse(...)` TODO comment.
- [ ] 4. Confirm `research_subagent`'s in-process call to the Research graph still works once Research's tools are Gateway-backed (post-F4/F5) — this call bypasses the Router entirely (B71), so it needs its own Gateway-scoped token; determine and implement how Drafting's runtime obtains a token covering Research's tool needs (this was flagged as an open question in `agents.md` §2/§7 — resolve it as: Drafting's own AgentCore Runtime is a specialist agent per B26/B30 and already gets all 10 source scopes, so no separate token request should be needed for the in-process Research call; confirm this in testing rather than assuming).
- [ ] 5. Add the Export outputs S3 bucket to `backend-stack.ts` (private, no public access, presigned GET only, AWS-managed encryption per B52).
- [ ] 6. Modify `shared/tools/export.py` and the format-specific export modules to write to the S3 bucket instead of local `output_dir`, returning an `export_id`/S3 key instead of a local path.
- [ ] 7. Add the review-acknowledgment gate per B81: `export_documents`/the client-initiated export route must not produce a final download until the export dialog's "I have reviewed this output" checkbox has been checked for that turn; the Export action is disabled until then, and the acknowledgment is logged (user, turn, timestamp) — implement as a required flag/field checked before the export step runs, using an existing confirm-style component from the CEI UI 1.1 catalog rather than inventing a new one.
- [ ] 8. Write `exports-handler`: `GET /exports/{exportId}/download` returns a presigned GET URL (15 min TTL, B78); `POST /sessions/{sessionId}/turns/{turnId}/export` (B74) lets a client request an export of an already-completed answer, format specified in the request body, reusing the same export pipeline the agent uses internally.
- [ ] 9. Confirm all six formats (PDF, DOCX, MD, CSV, LaTeX, TXT — B61 keeps TXT in the allowlist) render correctly against the live S3-backed pipeline; PDF/DOCX specifically need a live Code Interpreter session (`_generate_via_ci`), confirm this works against the now-granted Code Interpreter access from task 3.

## Acceptance criteria

- A Drafting run produces a real document, grounded and reviewed, and each requested export format downloads correctly via a presigned URL with a 15-minute expiry.
- An export attempt with the "I have reviewed this output" checkbox unchecked is blocked (Export action stays disabled); checking it proceeds and the acknowledgment is logged (B81).
- A client can request `POST .../export` for a turn whose answer already completed, independent of whether the agent produced an export during the original run, and receives a working download (B74's second export trigger).
- `research_subagent`'s in-process Research call succeeds using Drafting's own specialist-agent Gateway scopes, with no separate Router round-trip.
- `npx cdk synth` passes cdk-nag with ADR-format suppressions only.

## Tests

- `pytest`: export pipeline unit tests per format (extend `backend/agents/tests/test_drafting_nodes.py`-style patterns) against the S3-backed write path (mocked S3), the review-acknowledgment gate (blocks without it, proceeds with it), and the `research_subagent` in-process call against a mocked Gateway-backed Research tool.
- `pytest`/scripted for `exports-handler`: presigned URL generation, TTL correctness, `POST .../export` request validation and successful export-pipeline invocation, cross-user isolation (a user cannot download another user's export by guessing an `exportId`).
- CDK assertion tests: Export bucket is private with no public access; Drafting `AgentCoreRuntime` has Code Interpreter `grantUse` permissions attached.
- Integration test: full Drafting run → review acknowledgment → export in each of the six formats → download via presigned URL, against a real deployed environment.

## Risks and open items

- `api.md` explicitly lists as not-decided "whether a client-initiated ad hoc export route exists, versus agent-produced exports only" — B74 resolves this (both exist); if the `api.md` file you read still shows it as open, follow B74, not the older spec text (the parallel spec-update agent may not have applied B74 to this file yet).
- Drafting's `revise` HITL design (interrupt vs. plain re-invoke, `agents.md` §2/§8 open item) is explicitly deferred to F13/B72 — do not build an `interrupt`-based revise loop in this phase; keep today's plain-re-invoke `revision_feedback` field working as-is.
- B81 fixes the review-acknowledgment's wording ("I have reviewed this output"), placement (the export dialog), disabled-until-checked behavior, and logging requirement. The one remaining implementation detail is which existing CEI UI 1.1 component carries the checkbox — if no existing allowlisted component fits cleanly, flag this to the user rather than inventing a new component type outside the 1.1 catalog.

## Handoff notes

- F8 (Model + mode controls) adds the per-chat model picker and mode toggle that Drafting's runtime must also respect, same as Research.
- F13 (HITL) will eventually replace Drafting's plain-re-invoke `revise` loop with a real `interrupt`, per B72 — when that lands, this phase's export-gate logic and Drafting graph structure should not need to change, only the revise node's HITL mechanism.
- F19 (Data deletion) must also delete a user's exports from the Export outputs S3 bucket as part of the chat-delete cascade — this phase's bucket/key scheme should be recorded here once implemented so F19 can target it precisely.
