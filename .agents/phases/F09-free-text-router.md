# F09 — Free-text router

Status: not started | Depends on: F5 | Decisions: B15, B17, B80 | Specs: [`../specs/agents.md`](../specs/agents.md) §5–6 (Router classifier contract), [`../specs/api.md`](../specs/api.md) (`/chat/stream`)

## Goal

Let a user type free text into the chat (not just launch a dashboard task) and have the Session Router pick the right specialist agent automatically, using a small, fast, fixed classifier model — independent of the user's own chat model selection.

## Scope — In / Out

**In:** the classifier-model call inside `backend/lambdas/session-router/` for free-text requests, the structured-output contract `{agent, confidence}`, routing to any of the four agents that exist at this phase's implementation time (Research and Drafting are live per F5/F7; Analysis and Pipeline Manager may still be unimplemented — route to them anyway per the classifier's output, and let their own `NotImplementedError` gaps surface until F14/F17 land, rather than special-casing them out of the classifier); the low-confidence fallback per B80 — when the classifier's confidence is below a threshold, the Router responds with a `ChoicePicker` surface ("Which kind of help do you want?") listing the four agents, and the user's choice routes the request.

**Out:** any change to the deterministic dashboard-task lookup path (F5's mechanism is unchanged — this phase only adds a second path for free text), building Analysis/Pipeline Manager themselves (F14/F17), the exact numeric confidence threshold (B80 leaves this as "an implementation default, tune in F9" — pick a starting value and record it, do not treat it as decided by the log).

## Deliverables

- `backend/lambdas/session-router/` (modify, from F5) — add: detect whether the incoming `/chat/stream` request is a dashboard-task request (has a task-type field/deterministic match) or free text (no task type); for free text, call a small fixed Bedrock model with structured output `{agent: "research"|"drafting"|"analysis"|"pipeline_manager", confidence: float}`, independent of the user's own selected chat model (B17: "The classifier model does not depend on the user's model selection").
- `backend/agents/shared/config.py` (modify) — add a `router_classifier_model_id` config value, separate from `default_model_id`, fixed and not user-selectable.
- `backend/lambdas/session-router/` test fixtures (new) — a set of representative free-text prompts with expected agent labels, used both as a unit-test fixture and as a basis for measuring classifier accuracy during implementation.
- `backend/lambdas/session-router/` (modify) — the low-confidence path (B80): build a `ChoicePicker` surface listing the four agents, end the turn with it (a normal chat-turn response, not a LangGraph `interrupt`/`ui-action` resume, since the Router itself is a plain Lambda, not a graph — this is an implementation choice, not a decision-log fact); the user's next chat message carries the chosen agent, and the Router routes directly to it without re-running the classifier.

## Tasks

- [ ] 1. Add the dashboard-task-vs-free-text detection to the Router's request handling (a task-type field present → deterministic lookup, unchanged from F5; absent → classifier path).
- [ ] 2. Add `router_classifier_model_id` to `shared/config.py`, sourced from `deploy.sh`'s config (a small, fast model, chosen independently of any user-facing model list/blocklist from F8).
- [ ] 3. Write the classifier call: structured-output request to the fixed model, returning `{agent, confidence}`; route the request to the corresponding AgentCore Runtime the same way the deterministic path does (same M2M token request shape, same scopes per B26/B30 since the target is still a specialist agent).
- [ ] 4. Handle the case where the classifier targets an agent that is not yet implemented (Analysis/Pipeline Manager, before F14/F17 land) — the request should route through normally and surface whatever error the unimplemented graph's `NotImplementedError` produces, redacted per B7, not a special Router-level message that leaks implementation status.
- [ ] 5. Build the test-fixture set of free-text prompts with expected labels; use it to both unit-test the classifier call's plumbing (with a mocked model response) and, separately, to spot-check real classifier accuracy against a live model call during implementation (the accuracy spot-check is not a blocking automated test, but should be recorded in this phase's notes once run).
- [ ] 6. Confirm the classifier's own model call still goes through Guardrails if F8 has landed by this point; if F8 has not landed yet, note this explicitly as a temporary gap rather than silently skipping it.
- [ ] 7. Implement the B80 low-confidence path: pick a starting confidence threshold (record the chosen value here once implemented), and below it, return a `ChoicePicker` surface ("Which kind of help do you want?") naming the four agents instead of routing directly; treat the user's next message as carrying the chosen agent and route to it without a second classifier call.

## Acceptance criteria

- A free-text `/chat/stream` request with no task-type field is routed to a specialist agent chosen by the classifier's structured output, not by the deterministic lookup table.
- The classifier's model choice is fixed and does not vary with the user's own selected chat model (verify by testing with two different user-selected models against the same free-text prompt and confirming the classifier call is identical either way).
- A dashboard-task request (task-type field present) still routes exactly as it did after F5 — this phase changes nothing about that path.
- The fixture set of representative prompts routes to the expected agent for at least the clear-cut cases (ambiguous prompts are allowed to have lower confidence; document any prompts that route "wrong" rather than silently tuning the fixture to hide them).
- A free-text prompt whose classifier confidence falls below the chosen threshold receives a `ChoicePicker` surface listing the four agents (B80), not a silent best-guess route.

## Tests

- `pytest`: Router's task-vs-free-text detection logic; classifier call plumbing (mocked model response) for each of the four possible `{agent, confidence}` outputs; routing to an unimplemented agent surfaces a redacted, non-leaking error.
- `pytest`: fixture-based classification test using a mocked model response matching each fixture prompt's expected label (this tests the Router's handling of the label, not the model's real accuracy).
- `pytest`: a below-threshold confidence value returns the `ChoicePicker` surface (B80) instead of routing; a follow-up message naming the chosen agent routes directly to it without a second classifier call.
- Manual/recorded (not a blocking automated gate): a real classifier accuracy spot-check against the fixture set with the real fixed model, recorded in this phase's notes once run.

## Risks and open items

- B80 decides the low-confidence UX (a `ChoicePicker` listing the four agents) but leaves the numeric confidence threshold as "an implementation default, tune in F9" — pick a starting value, record it here, and expect to tune it once real classifier traffic is observed.
- This phase's `ChoicePicker` response is handled as a normal turn-ending surface, not a LangGraph `interrupt`/`ui-action` resume (the Router is a plain Lambda, not a graph) — this is a reasonable default, not a decision-log fact; if F13's HITL mechanism later needs the Router to use the same interrupt/resume pattern for consistency, that is a design question for whoever revisits this file, not an assumption to bake in silently now.
- Routing to Analysis/Pipeline Manager before F14/F17 exist means users could hit `NotImplementedError`-derived errors from real free-text requests during the window between F9 and F14/F17 shipping — if this phase is deployed to real users before F14/F17 land, flag this sequencing risk to the user rather than assuming it's fine.

## Handoff notes

- F14 (Analysis agent) and F17 (Pipeline Manager) do not need to change anything in this phase's Router code once they ship — the classifier already routes to them; their own implementation just stops producing `NotImplementedError`.
- F8 (Model + mode controls), if it lands after this phase, should double check the classifier's fixed-model call is still excluded from any user-facing model list/blocklist UI — the classifier model is never user-selectable.
