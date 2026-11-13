# F09 — Free-text router

Status: partial (backend built: classifier in the Session Router, threshold 0.7, buffered-to-SSE adapter, ChoicePicker, `route-choice` action; SPA free-text input in progress; not verified on AWS; see "Implementation notes") | Depends on: F5 | Decisions: B15, B17, B80 | Specs: [`../specs/agents.md`](../specs/agents.md) §5–6 (Router classifier contract), [`../specs/api.md`](../specs/api.md) (`/chat/stream`)

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

## Implementation notes (wave2/router, built on top of F8's model controls)

Implemented: `backend/lambdas/session-router/handler.py` wires `classifier.py` into the `start` op; the
threshold is `CEI_ROUTER_CONFIDENCE_THRESHOLD` (default `0.7`, `classifier.DEFAULT_CONFIDENCE_THRESHOLD`);
`backend/lambdas/session-runner/index.js` accepts a missing `task_type` and adapts non-Research agents'
buffered JSON into SSE; `backend/lambdas/ui-actions/index.js` adds the `route-choice` action. CDK:
`backend/lib/constructs/session-router.ts` takes `routerModelId`, `confidenceThreshold`, `guardrailArn`,
and `runtimeUrls` (all four agents); `backend/lib/backend-stack.ts` wires these from `agentRuntimes.invocationUrls`
and `modelGuardrail`, and grants UiActions `lambda:InvokeFunction` on the Router plus `GetItem`/`DeleteItem`
on the Active-Run table.

### Pending-choice storage

B80's low-confidence `ChoicePicker` ends the turn without starting any agent, so there is nothing for a
LangGraph checkpoint to hold. The pending choice is one `UpdateItem` into the existing **Active-Run table**
(`ActiveRunsStore`), not a new table: `Key={"user_id": <sub>, "record": f"route_choice#{session_id}"}`,
attributes `message`, `mode`, `surface_id`, `expires_at` (now + 600s). The Router (`store_pending_choice`)
is the only writer; `ui-actions` is the only reader, and deletes the record once it starts the chosen
agent. This table already carries the realtime/background leases and the Runner's `replay#...` rows, so a
`route_choice#...` row is one more record type in the user-keyed item family, not new infrastructure.

### SSE event sequence (`POST /chat/stream`, Session Runner)

Every turn still begins with `surface_begin` (CEI UI *transport* version `"1.2"`, matching Research's own
`ResearchEventStream.begin()` — this is separate from the surface's own embedded `"version"` field, `"1.1"`).

- **Research** (classified or `task_type: "live-research"`): unchanged — real SSE pass-through from the
  Research runtime (`stream: true`), the full existing event catalog.
- **Drafting / Analysis / Pipeline Manager** (classified, confidence >= threshold): the Runner calls the
  runtime's `op: run` (default) once, buffered, `Accept: application/json` — never SSE. The one JSON
  envelope is adapted into exactly one of:
  - completed (`status: "completed"` or no `status` with a string `text`, e.g. Research-shaped envelopes):
    `surface_begin` -> one `component_add` per component (parent-first walk of `envelope.ui`, mirroring
    `shared/streaming/research.py`'s `ResearchEventStream.components()`) -> `surface_end` (`final: true`).
    A `completed` envelope with no `ui` (e.g. Pipeline's "plan could not be built") gets a synthesized
    one-component `Text` surface instead of being dropped.
  - awaiting_input (a HITL pause — outline approval, scope confirmation, plan approval): `surface_begin` ->
    one terminal `hitl_request` whose body is `envelope.ui` spread directly (same shape Research's own
    clarify pause emits) — the client resumes it exactly as any other HITL pause, via `POST /ui-actions`.
- **ChoicePicker (B80, confidence < threshold)**: the Router's response carries `choice_required: true` and
  `surface_id`; no agent runs, no `gateway_token`. The Runner renders the surface itself (the Router cannot
  build a `shared.schemas.cei_ui` object — that package is not in the REST Lambda layer): `surface_begin`
  -> one terminal `hitl_request` whose body is a `ChoicePicker` surface, `root_id`/`id` `"router-choice"`,
  `interaction_id: "router-choice"`, one `ChoiceOption` per agent (`value` = `"research"` | `"drafting"` |
  `"analysis"` | `"pipeline_manager"`), `action: {action_id: "route-choice", intent: "select"}`.

### `POST /ui-actions` route-choice request/response

Request is an ordinary `ActionRequest` (`backend/agents/shared/schemas/cei_ui.py`) whose `interaction_id`,
`component_id` are both `"router-choice"`, `action_id` is `"route-choice"`, and `value` is one of the four
agent strings:

```json
{ "request_id": "r1", "session_id": "...", "surface_id": "router-choice-<run_id>", "revision": 1,
  "interaction_id": "router-choice", "component_id": "router-choice", "action_id": "route-choice",
  "value": "drafting" }
```

`ui-actions` detects this action (before the existing `agent_type`/`RESUMABLE` lookup — the session has no
agent yet) and, after verifying ownership and the stored pending choice, calls the *Session Router* Lambda
(`op: start`, a fresh server-generated `run_id`, `request.route_agent: "drafting"`) the same way the Runner
does, then invokes the returned `runtime_url` directly (`op: run` default, buffered JSON, no SSE), then
`op: finish` to release the lease. The response is one JSON envelope, the same contract as an existing
`/ui-actions` resume (`result?`, `text`, `ui?`, `status`, `action_response`):

```json
{ "text": "...", "ui": { "...": "CEI UI surface..." }, "status": "completed",
  "action_response": { "request_id": "r1", "session_id": "...", "surface_id": "router-choice-<run_id>",
    "revision": 1, "interaction_id": "router-choice", "component_id": "router-choice",
    "action_id": "route-choice", "value": "drafting", "status": "completed", "message": null } }
```

An expired or missing pending-choice record returns `{"status": "rejected", "code": "expired", ...}`; a
`surface_id`/`revision` mismatch or an unrecognized `value` returns `{"status": "stale", ...}` — both `200`,
matching the existing resume envelope's `stale`/`rejected` statuses, not a REST error.

### `route_agent`: an internal-only field, not a client-facing one

`request.route_agent` on the Router's `op: start` payload skips the classifier and routes directly to the
named agent with the given `message`/`mode`. It is sent only by `ui-actions` (an IAM-authenticated internal
caller, same trust boundary as the Runner's `task_type: "live-research"`), never accepted from a browser —
`session-runner`'s own `validateRequest` does not pass it through from the client request body at all.

### Known gaps / follow-ups

- Drafting's `op: run` requires a valid `document_type`; a free-text route to Drafting has none, so both
  `session-runner` and `ui-actions` default it to `"whitepaper"` (duplicated in both files — no shared JS
  module exists between the two Lambda bundles today).
- The low-confidence threshold (`0.7`) and the pending-choice TTL (600s) are both implementation defaults
  per B80/CLASSIFIER.md, not tuned against live classifier traffic — this requires real AWS (a deployed
  Guardrail + the fixed classifier model) and is out of scope for this environment.
- A real classifier accuracy spot-check against `tests/fixture_prompts.py` with the live fixed model (F09
  task 5's non-blocking manual step) also requires a deployed Bedrock Guardrail/model and was not run here.

### Needs live AWS verification

- A free-text `/chat/stream` turn with no `task_type` calls the classifier and routes to the expected agent.
- The classifier call is the same for two chats with different user-selected models.
- A below-0.7 prompt gets the `ChoicePicker` as a terminal `hitl_request`.
- A `route-choice` pick starts the chosen agent with the original message and deletes the pending record.
- A pick after 600 seconds returns `status: rejected`, `code: expired`.
- Drafting, Analysis, and Pipeline Manager answers arrive as SSE through the Runner adapter.
- A Guardrail block returns `400`; a classifier failure returns `503` and mints no token.
- The real classifier accuracy spot-check against the fixture set.
