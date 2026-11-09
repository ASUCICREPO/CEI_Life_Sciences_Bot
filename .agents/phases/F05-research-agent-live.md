# F05 — Research agent live

Status: in progress (isolated foundations; production route and live validation pending) | Depends on: F3, F4, S1 | Decisions: B3, B15, B17, B22, B23/B79 (10-minute threshold, decided), B26, B28, B30, B85, B100 | Specs: [`../specs/agents.md`](../specs/agents.md) §1 (Research Agent), §6 (cross-agent mechanisms), §7 (gap table), [`../specs/architecture.md`](../specs/architecture.md) §2.1, [`../specs/api.md`](../specs/api.md) (`/chat/stream`), [`../specs/streaming-protocol.md`](../specs/streaming-protocol.md) (base transport, not yet the full 1.2 UI wiring — that's F6)

## Goal

Deploy the existing Research agent graph to AgentCore Runtime, build the real Node.js Session Runner and the deterministic Session Router, and make `/chat/stream` return a real, streamed Research answer end to end for a dashboard-task request. This is the first phase where a user's request actually reaches a live agent.

## Scope — In / Out

**In:** `/chat/stream` route (STREAM) on the existing REST API, the Session Runner Lambda (Node.js), the Session Router Lambda (deterministic lookup only — the free-text classifier is F9), the Research AgentCore Runtime deployment, wiring `AgentCoreMemorySaver` into the Research graph (replacing `InMemorySaver`), the per-user real-time concurrency limit (2 concurrent runs, B22), and the async-threshold check using B79's decided value (10 minutes / 600s: if the `triage` step's plan estimate exceeds it, the run goes async instead of streaming — there is no mid-run handoff; `job_handoff` only ever means "this run started as a background job," decided before any streaming began, never a live switch partway through an SSE stream).

**Out:** the full CEI UI 1.2 progressive SSE surface (F6 — this phase can ship a simpler "one shot at the end" or partial progressive stream as an interim step, but the full patch-event catalog is F6's deliverable); the free-text classifier path (F9); Drafting/Analysis/Pipeline Manager (F7/F14/F17); background job handoff's real Job ID Store (F16 — this phase only emits the `job_handoff` event shape if B79's threshold is exceeded, pointing at a not-yet-built Jobs API, clearly flagged); any mid-run handoff from streaming to async (B79 rules this out entirely — the decision is made once, before streaming starts).

## Deliverables

- `backend/lambdas/session-runner/` (new, Node.js/TypeScript) — the STREAM-mode Lambda handling `/chat/stream`, relaying `InvokeAgentRuntime` chunks from the Router→Runtime path as SSE, using S1's confirmed framing.
- `backend/lambdas/session-router/` (new, Python) — deterministic task→agent lookup (Goals 1–3 dashboard tasks map to Research/Drafting/Analysis; Pipeline Manager for Goal 4), requests a scoped M2M token from F4's resource server, invokes the target AgentCore Runtime.
- `backend/agents/main.py` (modify) — replace `InMemorySaver` with `AgentCoreMemorySaver` (`backend/agents/shared/memory/agentcore.py`, already exists but unwired), remove the hand-rolled LRU eviction now that the checkpointer is externalized.
- `backend/agents/research/graph.py`, `research/nodes.py` (modify) — no logic change required to the existing `triage → retrieve → synthesize → cite` graph, but `retrieve`'s `DataSourceTool.invoke()` calls must switch from direct HTTP to the Gateway MCP call path F4 built, using the Router-issued token.
- `backend/lib/backend-stack.ts` (modify) — add the `/chat/stream` resource/method (`responseTransferMode: STREAM`) to the existing `RestApi`, the Research `AgentCoreRuntime` construct (CodeZip asset per B48), and the Active-run counters table (`data-model.md` §6 — proposal: separate table, this phase can implement it as proposed there).
- `backend/agents/shared/config.py` (modify) — add a `model_id` passthrough parameter usage at the Research call site (mechanism only; the real picker/blocklist UI is F8) and an `async_threshold_seconds` config value defaulted to `600` (10 minutes, B79's decided value) — a config knob, not hardcoded, but `600` is the real decided default, not a placeholder.

## Tasks

- [ ] 1. Deploy the existing Research graph (`backend/agents/research/`) to an AgentCore Runtime, as a CodeZip asset per B48, added to `backend-stack.ts`.
- [ ] 2. Wire `AgentCoreMemorySaver` into `main.py`'s graph compilation, replacing `InMemorySaver`; confirm `thread_id=session_id`, `actor_id=user_id` per `data-model.md` §9.
- [ ] 3. Switch `research/nodes.py`'s `retrieve` node from `_direct_invoke` to the Gateway MCP call path, using the token the Router passes through the runtime invocation (per `auth-and-tool-scope.md` §5's token flow) — the agent forwards the token unchanged, it never reads or modifies scope claims.
- [ ] 4. Write the Session Router Lambda: deterministic task→agent lookup table (dashboard task type → agent), request a client-credentials token scoped to all 10 source scopes (specialist agent per B26/B30) with `sub`/session ID injected via `aws_client_metadata`, invoke the Research Runtime. (Once F15 ships the NCBI scope, add `cei-tools/src.ncbi` to this same request per B85 — specialist agents get NCBI access too, not just tool agents.)
- [ ] 5. Re-point F4's Router-M2M-client IAM restriction (task 2 of F04) from its placeholder role to this Router Lambda's real execution role.
- [ ] 6. Write the Session Runner Lambda (Node.js): accept `POST /chat/stream`, call the Router, relay the Router/Runtime's streamed chunks as SSE using S1's confirmed event framing (this phase can emit a minimal event set — `surface_begin`/`text_delta`-equivalent/`surface_end`/`error` — full 1.2 catalog is F6).
- [ ] 7. Add the `/chat/stream` STREAM route to the existing `RestApi`, reusing the same Cognito authorizer as every other route (per S1's confirmed combination).
- [ ] 8. Implement the async-threshold check: compare the `triage` step estimate (source count/step count) against 600 seconds. Until F16 provides jobs, return a recoverable explanation that background processing is unavailable, without a job handoff or broken link (B100). Decide once before streaming; there is no mid-run handoff (B79).
- [ ] 9. Implement the per-user real-time concurrency limit: increment/decrement the Active-run counters table (`data-model.md` §6) around each run, rejecting a third concurrent real-time run with `concurrency_limit` per `api.md`'s error table.
- [ ] 10. End-to-end manual test: a logged-in user submits a dashboard research task, sees a real answer streamed back, backed by real Gateway tool calls.

## Acceptance criteria

- A Cognito-authenticated `POST /chat/stream` request for a Research-mapped dashboard task returns a real, tool-grounded answer streamed as SSE, not a canned/local response.
- The Research graph's tool calls go through the AgentCore Gateway with a Router-issued, correctly-scoped token — confirmed by checking Cedar/Gateway logs show the expected scope, not a bypass.
- `AgentCoreMemorySaver` checkpoints are written per turn; a second turn in the same session can be started and the graph resumes with prior state.
- A user attempting a 3rd concurrent real-time run receives `concurrency_limit`.
- The async-threshold (600s, B79)/`job_handoff` mechanism is exercised in a test (even though the receiving Jobs API does not exist yet); the threshold value itself is treated as decided, only the receiving Jobs API is flagged as not-yet-built.

## Tests

- `pytest` unit tests for the Router's deterministic lookup table (task type → agent mapping) and for the modified `retrieve` node (Gateway call path, using a mocked Gateway client).
- Jest unit tests for the Session Runner Lambda: SSE framing, error redaction (reuses the existing rules in `shared/transport/sse.py`, ported/mirrored on the Node.js side).
- CDK assertion tests: `/chat/stream` method has `responseTransferMode: STREAM` and the Cognito authorizer; the Research `AgentCoreRuntime` construct exists.
- Integration test (scripted): full request → Router → Runtime → Gateway → SSE response, against a real deployed environment, asserting the final answer contains at least one citation grounded to a real source Lambda call.
- Concurrency limit test: fire 3 real-time requests for the same user near-simultaneously, assert the 3rd is rejected.

## Risks and open items

- B79 has resolved the async threshold to 600s (10 minutes) — this phase ships the real decided value, not a placeholder. Only the *receiving* Jobs API (F16) is still not built.
- The `job_handoff` event this phase can emit has no receiving Jobs API until F16 — a client built against this phase alone would see a dead end if the threshold is ever crossed; document this clearly so F6's frontend work does not assume Jobs routes exist before F16 ships.
- B79 explicitly rules out any mid-run handoff: once a run starts streaming, it never switches to async partway through. Do not build any "upgrade to async mid-stream" logic.
- `agents.md` §7's gap table lists "Prompt caching," "Guardrails," and "Model selection" as unimplemented — this phase does not need to add them (F8 owns them), but should not block on their absence either.

## Handoff notes

- F6 (Streaming UI) replaces this phase's minimal SSE event set with the full CEI UI 1.2 catalog (`surface_begin`/`component_add`/`text_delta`/`grounding_update`/`trace_event`/etc.) on top of the same Session Runner/Router/Runtime wiring this phase establishes — F6 should extend, not rewrite, `backend/lambdas/session-runner/`.
- F9 (Free-text router) extends `backend/lambdas/session-router/` with the classifier-model path; the deterministic lookup this phase builds stays unchanged for dashboard-task requests.
- F16 (Background jobs) builds the real Jobs API this phase's `job_handoff` mechanism points at, reusing B79's already-decided 600s threshold (no threshold decision left to make).
- F15 (Tool agents), once it adds the `cei-tools/src.ncbi` scope, means this phase's Router should be revisited to add that scope to every specialist-agent token request it issues (B85).
- F7/F14/F17 (Drafting, Analysis, Pipeline Manager going live) each repeat this phase's deployment pattern (AgentCore Runtime + `AgentCoreMemorySaver` + Router lookup entry) for their own agent.
