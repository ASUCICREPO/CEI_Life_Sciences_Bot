# F06 — Streaming UI

Status: in progress (Research and Drafting CEI UI 1.2 streams, Runner with replay, thinking block and trace route built; Analysis and Pipeline streams and deployed checks pending) | Depends on: F5 | Decisions: B4, B5, B6, B7, B9, B10, B59, B60, B68, B78 | Specs: [`../specs/streaming-protocol.md`](../specs/streaming-protocol.md) (full), [`../specs/data-model.md`](../specs/data-model.md) §2 (trace storage), [`../specs/tools.md`](../specs/tools.md) §6 (Artifact/presenters)

## Goal

Ship the full CEI UI 1.2 progressive SSE protocol end to end: the Session Runner emits the full event catalog, the frontend renders the surface as it builds (not all at once at the end), the collapsible thinking block shows the live trace, grounding shows a "verifying" pending state before final status, and every trace event is persisted (compressed/chunked over 400 KB) for later lazy loading.

## Scope — In / Out

**In:** the full event catalog from `streaming-protocol.md` (`surface_begin`, `component_add`, `text_delta`, `component_update`, `grounding_update`, `trace_event`, `progress`, `usage`, `heartbeat`, `surface_end`, `error` — `hitl_request`/`job_handoff` event *shapes* are included since they terminate a stream, but their real trigger conditions belong to F13/F16), the frontend renderer that consumes these events and builds the component graph live, the thinking block UI (B6), the trace persistence schema in Chat History Store including gzip+chunking over 400 KB (B9/B78: 350 KB chunk size), the `GET /sessions/{id}/turns/{messageId}/trace` route (lazy load, B59), and the stream reconnect/resume mechanism (B68: sequence-numbered events written to Chat History Store, client reconnects with last seen `seq`).

**Out:** HITL's real pause/resume trigger conditions (F13 — this phase only needs to render a `hitl_request` surface if one arrives and end the stream correctly; it does not add any node that calls `interrupt`), the async `job_handoff`'s real Jobs API (F16 — same treatment as F5: render correctly if it arrives, don't build the receiving side), Drafting/Analysis/Pipeline Manager's own surfaces (F7/F14/F17 reuse this phase's renderer, they don't change it).

## Deliverables

- `backend/lambdas/session-runner/` (modify, from F5) — emit the full 1.2 event catalog: `component_add` per presenter-built component, `text_delta` for streaming text fields (4 KiB soft cap, B78), `grounding_update` (`pending` then final status per B10), `trace_event` for every plan/tool-call/source-access/grounding-decision/reasoning entry (real detail, B7's never-show list still enforced), `progress`, `usage`, sequence-numbered `id:` frames written to `ChatHistoryStore` as they're sent (B68).
- `frontend/src/lib/cei-ui/` (new, ported/adapted from `archive/frontend-next/lib/cei-ui/` per B66 — the schema and renderer logic already exists there for CEI UI 1.1; this phase adds the 1.2 streaming client on top: `fetch`+`ReadableStream` consumption, event-to-component-graph reducer, ordering-rule enforcement from `streaming-protocol.md`).
- `frontend/src/components/chat/ThinkingBlock.tsx`, `frontend/src/components/chat/StreamingSurface.tsx` (new) — the collapsible thinking block (expanded while running, collapses at `surface_end`, re-expandable, loads persisted trace lazily) and the live-building surface renderer.
- `backend/agents/shared/persistence/chat_history.py` (modify) — add `record_type: "turn"|"trace_event"` handling, the gzip+chunk-over-350KB write/read path (B9/B78 chunk size, key schema `SK = "trace#<turn_sequence>#<trace_seq>#chunk#<n>"` + `#meta` sibling item, per `data-model.md` §2's proposal, now treated as the chosen shape since B78 fixes the chunk size).
- `backend/lambdas/session-history-handler/` (modify, from F3) — add `GET /sessions/{sessionId}/turns/{messageId}/trace`, reassembling chunked trace items in order and gunzipping.
- `backend/lambdas/session-runner/` (modify) — add the resume path: accept a last-seen `seq` (mechanism per B68 — exact route/field shape was a proposal in `streaming-protocol.md`/`api.md`; implement it as a field on the same `/chat/stream` request per B68's description, "client reconnects with its last sequence number," rather than inventing a separate route unless implementation reveals a REST-shape reason to split it out).
- `backend/agents/shared/transport/sse.py` (modify) — extend the existing redaction/framing helper to the richer `trace_event` shape (real tool names/inputs/outputs/sources/reasoning-text-labeled-summarized) while keeping the exact same never-show list (model IDs, guardrail/system prompts, prompt versions, secrets) — this is a widening of what's shown, not a loosening of what's protected, per `security.md`'s explicit framing.

## Tasks

- [ ] 1. Extend `shared/transport/sse.py` (or add a sibling module) to emit every 1.2 event type with the exact field shapes in `streaming-protocol.md`; keep the existing generic-placeholder behavior available as a fallback/flag if useful for a transition period, but the default becomes the richer `trace_event`.
- [ ] 2. Wire the Research presenter (`shared/presenters/cei_ui.py`) to emit `component_add`/`text_delta` incrementally as the `synthesize`/`cite` nodes produce output, instead of building the whole `ResponseDocument` and emitting it as one block at the end — this is the core "UI builds while the agent works" requirement (B4).
- [ ] 3. Add the grounding pending-state emission: immediately after a groundable component (`Table`/`StatGrid`/`Graph`/`Artifact` with `data`) is added, emit `grounding_update` with `status: "pending"`, then the final-status update once `shared/grounding.py` finishes for that block (B10).
- [ ] 4. Add sequence-numbered writes: every non-heartbeat event the Session Runner sends is also appended to `ChatHistoryStore` with its `seq`, per B68, so a reconnect can replay from there.
- [ ] 5. Implement the resume path in the Session Runner: accept the client's last-seen `seq`, replay missing events from `ChatHistoryStore` (or an in-run buffer, whichever proves simpler — record the choice here once implemented, since `streaming-protocol.md`'s own proposal was an in-memory buffer, while B68 decided the DynamoDB-backed replay; **follow B68**, not the older in-memory proposal), then continue live.
- [ ] 6. Implement the trace persistence schema in `chat_history.py`: `record_type: "trace_event"`, gzip, 350 KB chunk size (B78), `#meta` sibling item with `{total_chunks, total_bytes, compressed: true}`.
- [ ] 7. Add `GET /sessions/{sessionId}/turns/{messageId}/trace` to `session-history-handler`, reassembling chunks in order and gunzipping before returning the ordered trace-event log.
- [ ] 8. Port the CEI UI 1.1 schema/validator from `archive/frontend-next/lib/cei-ui/schema.ts` into `frontend/src/lib/cei-ui/`, and add the 1.2 streaming reducer on top (applies `component_add`/`text_delta`/`component_update`/`grounding_update` events into a live component tree, enforcing the ordering rules from `streaming-protocol.md`).
- [ ] 9. Build `StreamingSurface.tsx`: consumes the SSE stream via `fetch`+`ReadableStream` (not `EventSource`, since it can't send `Authorization` — B4), renders the live component graph, handles `hitl_request`/`job_handoff`/`error` termination shapes (rendering only — no HITL/async logic yet).
- [ ] 10. Build `ThinkingBlock.tsx`: renders incoming `trace_event`s (especially `reasoning`/`plan`) while `progress.status` is `planning`/`running`/`verifying`, collapses at `surface_end`, and on re-expand after the fact, lazily fetches `.../trace` instead of relying on the live stream's buffer.
- [ ] 11. Verify B7's never-show list is enforced end to end: write a test that asserts no `trace_event` payload ever contains a model ID, guardrail config, system prompt, prompt version string, or a value matching a secret pattern.

## Acceptance criteria

- A live chat visibly builds component by component and streams text token by token; the UI is never "all at once at the end" for a normal Research answer.
- A groundable block shows a "verifying citations" pending state before its final grounding status appears.
- The thinking block is expanded during a run, collapses at completion, and can be manually re-expanded afterward, loading the persisted trace via the `/trace` route.
- A trace payload exceeding 400 KB is stored as multiple gzip-compressed DynamoDB items and reassembles correctly on read.
- A deliberately dropped connection mid-run, followed by a reconnect carrying the last-seen `seq`, resumes without duplicating or losing events.
- No `trace_event` (live or persisted) ever contains a model ID, guardrail/system prompt, prompt version, or secret value.
- A stored 1.2 surface, once fully replayed, is byte-for-byte valid under the 1.1 component schema (only `version` differs), per `streaming-protocol.md`'s compatibility requirement.

## Tests

- `pytest`: `chat_history.py`'s chunk/reassemble round-trip (including a payload deliberately larger than 400 KB), the `record_type` discrimination in `load_page`, and the SSE redaction test from task 11.
- `pytest`/scripted: the Session Runner's resume logic — start a stream, cut it after N events, reconnect with `seq=N`, assert only events after N are replayed and no duplicates occur (reuses the "Idempotency and sequence numbers" rule in `streaming-protocol.md`).
- Frontend (Vitest or the project's chosen test runner): the event-to-component-graph reducer — test each event type's effect on the tree, test the ordering rules (`component_add` before any event referencing it; grounding `pending` before final status), test that an out-of-order/duplicate `seq` is discarded per the idempotency rule.
- Frontend: a rendered-surface snapshot test comparing a fully-replayed 1.2 stream's final state against the equivalent stored 1.1 `ui` block, confirming schema compatibility.
- Integration/e2e: full real-time Research run observed through the frontend, confirming progressive rendering visually (manual) plus an automated assertion that at least N `component_add`/`text_delta` events were received before `surface_end` (i.e., not one giant final payload).

## Risks and open items

- `streaming-protocol.md` itself lists several proposals as not decided (max `text_delta` size — B78 resolves this to 4 KiB, follow the decision log; `usage` emission cadence; whether `grounding_update`'s pending state has an artificial minimum duration). Where the decision log has since resolved one of these (B78), follow the decision log's value even if the spec file you read still shows it as "proposed, not decided" — the parallel spec-update agent may not have finished editing that file yet.
- B68 changes the reconnect design away from `streaming-protocol.md`'s original "Session Runner in-memory buffer" proposal to a DynamoDB-backed sequence replay — implement B68's version; do not build the in-memory buffer described in the older text.
- The `hitl_request`/`job_handoff` rendering this phase adds is inert until F13/F16 land — confirm the frontend does not crash or hang if one of these events is somehow received before those phases exist (it shouldn't happen given F5's scope, but the renderer should fail safely regardless).

## Handoff notes

- F7 (Drafting + export), F14 (Analysis agent), F17 (Pipeline Manager) all render through this phase's `StreamingSurface.tsx`/`ThinkingBlock.tsx`/event reducer without modification — they only need their own agent's presenter to emit the same event catalog this phase defines.
- F13 (HITL) makes `hitl_request` a real, reachable event (via an actual `interrupt` in the graph); it should not need to change this phase's rendering of that event, only its production trigger and the `/ui-actions` resume flow.
- F16 (Background jobs) makes `job_handoff` real; same relationship as F13 above.
- F10 (Attachments + RAG) and F8 (Model + mode controls) both add new `trace_event`/`component` content (KB retrieval citations, model/mode indicators) that must fit inside this phase's existing event catalog and never-show list without needing new event types.
