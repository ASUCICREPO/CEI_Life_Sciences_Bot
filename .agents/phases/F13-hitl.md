# F13 — HITL
Status: not started | Depends on: F6, S4 | Decisions: B28, B56, B57, B60, B72, B82 | Specs: ../specs/agents.md, ../specs/streaming-protocol.md, ../specs/api.md

## Goal
Make the four inline HITL tools (`ask_user_question`, `present_alternatives`, `request_plan_approval`, `get_user_input`) really pause a running agent and really resume it later, using a LangGraph `interrupt` and an `AgentCoreMemorySaver` checkpoint, with a very long but not infinite wait (B56, B57, refined by B82: the wait lasts up to AgentCore Memory's maximum short-term event expiry, expected 365 days — verify the real value — after which the checkpoint is gone and a resume attempt must show "expired, start again"). Add the production `POST /ui-actions` endpoint that resumes a paused run.

## Scope — In / Out

**In**
- Convert the four inline tools in `shared/tools/hitl/inline_functions.py` to call LangGraph's `interrupt()` (instead of just returning a dict) so the graph actually pauses.
- The `hitl_request` SSE event (B60, defined in `streaming-protocol.md`) that ends a stream when a pause happens.
- The `POST /ui-actions` Lambda that validates an `ActionRequest`/returns an `ActionResponse` (both already defined in `shared/schemas/cei_ui.py`) and resumes the saved graph from its checkpoint.
- Wiring the mechanism into Research (clarifying question when triage selects zero sources) and Drafting (outline approval before the full draft; revision requests resuming into the revise loop) — the two agents whose node bodies already exist (B72).
- The "waiting for you" UI state (client-side), plus the B82 expiry fallback: if the checkpoint has passed AgentCore Memory's maximum short-term event expiry, `POST /ui-actions` returns a clear "expired, start again" response instead of a generic error.

**Out**
- Wiring the Analysis scope-confirmation HITL point or the Pipeline Manager plan-approval HITL point into those agents' node bodies — those bodies do not exist yet (`raise NotImplementedError` today). This phase delivers the shared mechanism only; F14 and F17 call it once their nodes exist (see "Risks and open items" for why this is a decision-log gap, not an oversight of this file).
- Tool Agent runtime HITL (not mentioned in B72; the Tool Agent does not exist until F15).
- Async/background-job HITL (checkpoint-then-stop-session-then-resume-in-new-session) — that needs F16's async session mechanism and is explicitly deferred to F16/F17.

## Deliverables
- `backend/agents/shared/tools/hitl/inline_functions.py` — exists (partial: builds the four `CEIUISurface` objects, returns them as plain dicts). Wrap each `_ask_user_question`/`_present_alternatives`/`_request_plan_approval`/`_get_user_input` body with `langgraph.types.interrupt(surface.model_dump(mode="json"))` so the tool call pauses the graph; the resumed value becomes the tool's return value.
- `backend/agents/research/nodes.py` — add a `clarify` node: when `triage` selects zero sources, call `ask_user_question` instead of the current silent PubMed fallback (agents.md §1 flags this as the open HITL point).
- `backend/agents/research/graph.py` — add the `clarify` node and a conditional edge from `triage`.
- `backend/agents/drafting/nodes.py` — add an `approve_outline` node before `draft`, and change `revise`'s trigger from a plain state field (`revision_feedback`) to an `interrupt`-based request (agents.md §2 flags this as an open Proposal — this phase resolves it as "use `interrupt`", per B72's explicit wording "revision requests through an interrupt that resumes into the revise loop").
- `backend/agents/drafting/graph.py` — add `approve_outline` and its edges.
- `backend/lambdas/ui-actions/handler.py` (new) — implements `POST /ui-actions` per `api.md`: validates `ActionRequest`, checks `revision` against the stored surface's current revision (returns `stale` on mismatch), and resumes the graph via `graph.ainvoke(Command(resume=value), config=...)` against the `AgentCoreMemorySaver` checkpoint for that `thread_id`.
- `backend/lib/backend-stack.ts` — add the `POST /ui-actions` route (BUFFERED), its Lambda, and IAM to invoke the relevant AgentCore Runtime.
- `backend/agents/shared/transport/sse.py` — extend `EventType`/`SSEStream` (or hand this off cleanly to F6, whichever lands first) with a `hitl_request` event that carries the full surface and ends the stream, per `streaming-protocol.md`.

## Tasks
- [ ] 1. Add `interrupt()` calls to all four inline HITL tools; keep the surface-building code unchanged.
- [ ] 2. Add Research's `clarify` node and wire it after `triage` when zero sources are selected.
- [ ] 3. Add Drafting's `approve_outline` node before `draft`.
- [ ] 4. Change Drafting's `revise` trigger from a plain state field to an `interrupt`-based request; keep the existing revision-loop routing logic (`should` conditional in `drafting/graph.py`) but change what feeds it.
- [ ] 5. Confirm (against F5/S4's checkpointer wiring) that `AgentCoreMemorySaver` is what both agents use before this phase ships — this phase assumes F5 has already replaced `InMemorySaver`; if not, coordinate directly rather than building HITL on a checkpointer that will be replaced under it.
- [ ] 6. Build the `POST /ui-actions` Lambda: request validation, `revision` staleness check, checkpoint-based resume, `ActionResponse` construction.
- [ ] 7. Add the `/ui-actions` route + Lambda + IAM to `backend-stack.ts`.
- [ ] 8. Add the `hitl_request` SSE event (coordinate with F6; do not duplicate if F6 has already added it).
- [ ] 9. Add the B82 long-wait behavior: confirm no application-level TTL/expiry is added on top of AgentCore Memory's own maximum short-term event expiry (verify the real value; B82 expects 365 days); make `POST /ui-actions` detect a resume attempt against an expired/missing checkpoint and return a clear "expired, start again" response rather than a generic error or crash.

## Acceptance criteria
- A Research run where `triage` selects zero sources ends its stream with a `hitl_request` event carrying a `ChoicePicker` question, and the checkpoint is present in the checkpointer store afterward.
- Submitting a valid `POST /ui-actions` for that paused run resumes the graph and it completes normally in a new run.
- Submitting a `POST /ui-actions` with a stale `revision` gets `status: "stale"` and does not resume anything.
- A Drafting run pauses for outline approval before the full draft is generated, and pauses again on a revision request, resuming into the revise loop each time.
- No code path imposes an application-level timeout on a paused run shorter than AgentCore Memory's own maximum short-term event expiry (B57/B82); a resume attempt after that expiry returns "expired, start again," not a crash or silent failure.

## Tests
- Unit (pytest): each of the four HITL tools raises/returns through `interrupt` correctly under a test harness that fakes an interrupt boundary.
- Unit: Research `clarify` routing (zero sources → clarify; non-zero → retrieve, unchanged).
- Unit: Drafting `approve_outline` and `revise`-via-interrupt routing.
- Unit: `POST /ui-actions` handler — valid resume, stale revision, unknown session, malformed value (all bounded by `ActionRequest`'s existing validators).
- CDK assertion + cdk-nag: the `/ui-actions` route, its Lambda, and its IAM policy.
- Integration: a two-invocation test (pause, then a separate resume invocation) proving the checkpoint survives across process boundaries, not just in one Python process.

## Risks and open items
- **Resolved:** the decision-log Phase list and this roadmap now list F14 (Analysis) as depending on F6 **and** F13, matching B72's requirement that Analysis's scope-confirmation HITL point needs this phase's `interrupt` mechanism. F14's own file no longer needs a header-level correction.
- AgentCore Memory short-term event expiry maximum is set by B82 to the maximum available value (expected 365 days) — verify the real number during implementation. If the real value is shorter than expected, the "expired, start again" UI (B82) is the accepted fallback, not a sign that B56/B57's design needs to change.
- Denied-call behavior inside LangGraph when a Gateway call is Cedar-denied mid-run is a separate open item (`auth-and-tool-scope.md` §8) — not this phase's problem, but a paused-then-resumed run could hit it on resume; note for F4/S2 follow-up.

## Implementation status (2026-11-10)
Built on `complete-cei-workflows` (statically reviewed; Node tests pass locally, Python/Jest tests and deployment pending in CodeBuild):
- `POST /ui-actions` (`lambdas/ui-actions/index.js`, Node, STREAM mode, 14 min): exact `ActionRequest` field check, session ownership and the owning agent from `ChatMetadataStore.agent_type` (never from the request), then `op: "resume"` to that runtime with the caller's own bearer token (B102). Only Analysis and Pipeline pause today, so other chats get `invalid_request`. STREAM mode because a resumed analysis runs longer than a buffered integration's 29 s.
- Deployed SPA builds send actions there (`agentClient.sendAction` when `apiBaseUrl` is set); the local demo keeps the proxy `op: "resume"` path.
- B82 expiry: a cold resume with no checkpoint answers `status: "rejected"`, `code: "expired"`, "This request has expired. Start again." (`ops.expired_envelope`); a duplicate against an existing checkpoint stays `stale`.
Still open: tasks 1–4 (inline tools calling `interrupt()`, Research `clarify`, Drafting `approve_outline`/revise), `AgentCoreMemorySaver` for Analysis/Pipeline/Drafting (S4 unproven), and the two-process durability integration test.


- F14 (Analysis) and F17 (Pipeline Manager) must reuse this phase's `interrupt`-based pattern and the same `POST /ui-actions` endpoint — no new action endpoint should be built for them.
- F16 (background jobs) needs this phase's checkpoint-based pause primitive as the foundation for its "checkpoint, stop the session, resume in a new session" async HITL behavior (B25).
