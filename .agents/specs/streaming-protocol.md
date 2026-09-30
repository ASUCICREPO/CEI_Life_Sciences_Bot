# CEI UI 1.2 — Streaming Protocol Spec

Status: planned (S1, F5, F6). Source of truth is `.agents/decision-log.md`, decision B60: "CEI UI 1.2 adds progressive SSE patch events... Event names are a proposal; **the spec defines them**." This document is that spec. Where a design choice below is not fixed by the decision log, it is marked "Proposal (not decided)"; everything else is this spec's binding definition for the phases listed.

This spec extends, and does not replace, `docs/cei-ui-protocol.md` (CEI UI 1.1). 1.2 adds transport-level progressive delivery only. **It adds no new component to the 1.1 catalog.** The component allowlist, structural limits, artifact security model, grounding rules, and action contract in the 1.1 doc all apply unchanged; this spec cross-references them instead of repeating them.

Related specs (not owned here): `.agents/specs/api.md` (the `/chat/stream` and `/tools/{source}/chat/stream` routes this protocol rides on — B84), `.agents/specs/data-model.md` (where the final surface and the trace log are stored), `.agents/specs/agents.md` / `.agents/specs/tools.md` (what produces the events). This same event catalog and framing apply unchanged to the tool-agent stream route (B84); nothing in this document is `/chat/stream`-specific.

## Protocol identity

| Item | Value |
|---|---|
| Content type | `application/vnd.cei.ui+json` (unchanged) |
| Protocol | `cei-ui` (unchanged) |
| Version emitted by servers on new streams | `1.2` |
| Versions accepted by clients and stored history | `1.0`, `1.1`, `1.2` |
| Transport | SSE framing (`text/event-stream`) delivered over `fetch` + `ReadableStream`, not `EventSource` — `EventSource` cannot send an `Authorization` header, and the Cognito authorizer needs one (B4) |

A stream carries one surface at a time (the answer being built for the current turn). Once `surface_end` (or `hitl_request`/`job_handoff`/`error`) is emitted, the HTTP response ends.

## Version negotiation

- Servers always emit `version: "1.2"` in `surface_begin` and in the reconstructed final surface, for any newly started stream.
- Clients, the action logic, and stored chat history must accept `"1.0"`, `"1.1"`, and `"1.2"`. A validator must not reject a stored surface because it says an older version.
- 1.2 is a transport change only: the set of allowlisted components, their fields, and their limits are exactly the 1.1 catalog (`docs/cei-ui-protocol.md`). A 1.2 stream, once fully replayed by the client, produces one `CEIUISurface` object that is byte-for-byte valid under the 1.1 (and 1.0, where applicable) component schema — only the top-level `version` string differs.
- Non-streamed routes (for example `GET /sessions/{id}/history`, per `.agents/specs/api.md`) continue to serve whatever version a surface was stored at (`1.0`, `1.1`, or `1.2`); this spec does not require re-encoding old history.

## Event catalog

Every event is one SSE frame: `id: <seq>`, `event: <type>`, `data: <json>`, blank line. `seq` is a per-response monotonic integer starting at 0 (reuses the existing `id:`/`retry:` framing already implemented in `shared/transport/sse.py`; this spec adds no new SSE-level framing feature, only new `event` values and `data` shapes on top of it).

| Event | Purpose | Ends the stream? |
|---|---|---|
| `surface_begin` | Starts a new surface | no |
| `component_add` | Appends one component to the graph, in order (B4) | no |
| `text_delta` | Appends text to a streaming `Text`/`Markdown` component's content (B4, B10) | no |
| `component_update` | Patches fields on an existing component (used for grounding transitions, progress-driven relabeling) | no |
| `grounding_update` | Attaches citations / verification status to a component once grounding completes for it (B10) | no |
| `trace_event` | One ordered trace-log entry: plan step, tool call, source access, grounding decision, or reasoning text (B6, B7) | no |
| `progress` | Coarse-grained run status for the progress UI (B5) | no |
| `usage` | Live token/cost counter (B5) | no |
| `hitl_request` | A complete CEI UI interactive surface (`ReviewGate`/`ChoicePicker`/`TextField`) that pauses the run (B56) | **yes** (pause, not completion) |
| `job_handoff` | The run has been (or is being) moved to a background job (B23/B25) | **yes** (handoff, not completion) |
| `heartbeat` | Keep-alive; SSE comment, not a named event | no |
| `surface_end` | The surface is complete and stable | **yes** (normal completion) |
| `error` | A safe, redacted error | **yes** (abort) |

### `surface_begin`

```json
{"surface_id": "research-turn-42", "revision": 1, "content_type": "application/vnd.cei.ui+json", "protocol": "cei-ui", "version": "1.2", "root_id": "root"}
```

Always the first event of a stream. `revision` is `1` for a new answer; a `revision > 1` `surface_begin` would only occur if the server needs to replace an entire in-flight surface (not expected in normal operation — proposal, not decided, whether this ever happens).

### `component_add`

```json
{"surface_id": "research-turn-42", "revision": 1, "parent_id": "root", "index": 0, "component": {"id": "s0-b0-text", "type": "Text", "content": "", "variant": "body"}}
```

`component` is exactly one record from the 1.1 allowlist (`docs/cei-ui-protocol.md` "Component Allowlist"), with the same field rules and limits. `parent_id`/`index` tell the client where to splice it into the existing graph's children list. The root component itself is added with `parent_id: null`. A component that will receive `text_delta` events is added with an empty or partial `content`/`body` field; deltas fill it in.

**Ordering rule:** a component must be added before any `text_delta`, `component_update`, or `grounding_update` targets it, and before any later `component_add` references it as a parent.

### `text_delta`

```json
{"component_id": "s0-b0-text", "field": "content", "delta": "The review is comp"}
```

Applied in receive order to the named field (default `"content"`, but `Markdown.content`, `Callout.content`, and `EvidencePanel` passages also use this event against their own text-bearing field). The client appends `delta` to whatever it has already accumulated for that `component_id`/`field`. Deltas for a given component/field must arrive in order; the client does not reorder them (see "Idempotency and sequence numbers" below for how it detects a gap).

Maximum `delta` size per event: 4 KiB (decided, B78), a soft cap so no single SSE frame is disproportionately large.

### `component_update`

```json
{"component_id": "progress-badge", "patch": {"status": "verifying"}}
```

A shallow merge of `patch` into the component's existing fields. Used for anything that is not a `text_delta` append and not a `grounding_update` (which has its own event because it always carries citations/verification semantics — see below). Every field in `patch` must be a valid field for that component's type under the 1.1 schema; an update that would make the component invalid is rejected client-side and logged, not applied.

### `grounding_update`

```json
{
  "component_id": "s0-b1-statgrid",
  "status": "verified",
  "metrics": [{"index": 0, "verified": true}, {"index": 1, "verified": false}]
}
```

or, for a table:

```json
{"component_id": "s0-b2-table", "status": "partially_verified", "flagged_cells": [{"row": 1, "column": 1}]}
```

`status` is one of `pending`, `verified`, `partially_verified`, `unverified`, `dropped`, `estimate`. Sent once immediately after a groundable component (`Table`, `StatGrid`, `Graph`, `Artifact` with `data`) is added, with `status: "pending"` — this is the visual "verifying citations" state (B10). A second `grounding_update` follows once server-side grounding (`shared/grounding.py`) finishes for that block, carrying the final status and the same per-cell/per-metric detail already defined in `docs/cei-ui-protocol.md` ("Grounding of numeric content"): a sourced chart with any ungrounded value is `dropped` (the client removes the component and shows the trace note instead — see `trace_event` below); an estimate chart becomes `estimate`; ungrounded metrics/cells are `partially_verified` or `unverified` with the `†` marker rule unchanged.

**Ordering rule:** the `pending` `grounding_update` for a component always follows that component's `component_add` and precedes its final-status `grounding_update`, which itself follows the component's last `text_delta`/`component_update` (grounding runs only after a block finishes streaming, per B10).

### `trace_event`

```json
{
  "ts": "2026-09-29T00:00:03.210Z",
  "step_type": "tool_call",
  "tool_name": "pubmed___search",
  "tool_input": {"query": "GLP-1 weight loss RCT"},
  "tool_output_summary": "12 results retrieved",
  "sources": ["PMID:38291045"]
}
```

`step_type` ∈ `plan`, `tool_call`, `source_access`, `grounding_decision`, `reasoning`. A `reasoning` entry looks like:

```json
{"ts": "...", "step_type": "reasoning", "reasoning_text": "Checked two trials before concluding...", "reasoning_label": "summarized"}
```

`reasoning_label` is `"summarized"` when the model provider itself summarized its reasoning, or `"full"` when it did not (B7's exact wording). **Never present, per B7:** `model_id`, `guardrail_config`, `system_prompt`, `prompt_version`, or any secret/API key value. This is a behavior change from the current implemented SSE helper (`shared/transport/sse.py`), which today redacts thinking/tool detail entirely to generic `{"status": "processing"}` / `{"status": "tool_running"}` placeholders — 1.2's `trace_event` is intentionally richer, per B7, while keeping the same never-show list. Every `trace_event` is also the unit persisted to the Chat History Store trace log (`.agents/specs/data-model.md`); the live stream and the stored, lazily-loaded trace (B59) are the same feed, not two separate representations.

This is also how the "thinking block" (B6) is populated: the client renders incoming `trace_event`s (especially `step_type: "reasoning"` and `"plan"`) into the collapsible thinking block while `progress.status` is `planning`/`running`/`verifying`, and collapses it when `surface_end` arrives. The user can still scroll up and re-expand it afterward, now reading from the persisted trace via `.agents/specs/api.md`'s trace route.

### `progress`

```json
{"status": "running", "step_label": "Searching ClinicalTrials.gov", "current_step": 2, "total_steps": 5}
```

`status` ∈ `planning`, `running`, `verifying`, `finalizing`. `total_steps` is an estimate and can change; it is not a contract. This is the coarse status B5 asks for; `trace_event` is the detailed log underneath it.

### `usage`

```json
{"input_tokens": 1820, "output_tokens": 340, "total_tokens": 2160, "estimated_cost_usd": 0.0134}
```

Live counter (B5). Sent periodically (proposal, not decided: interval — every N tool calls or every M seconds; this spec proposes "on every `trace_event` of type `tool_call` and again at `surface_end`", not decided in the log).

### `hitl_request`

Data is a complete `CEIUISurface` object (the existing `ReviewGate`/`ChoicePicker`/`TextField` shapes from `shared/tools/hitl/inline_functions.py`, unchanged). The stream ends here without a `surface_end`. Per B56, the graph is checkpointed (`AgentCoreMemorySaver`) and the client must submit a typed action (`POST /ui-actions`, `.agents/specs/api.md`) to resume it. Per B57/B82, there is no fixed time limit on the wait; the chat shows "waiting for you" until the action is submitted or until the underlying AgentCore Memory short-term event reaches its maximum expiry (expected around 365 days, exact value to verify — `.agents/specs/data-model.md`), at which point the surface instead shows "expired, start again" and the paused run can no longer be resumed.

The Session Router's low-confidence routing question (B80) uses the same `ChoicePicker` shape as a `hitl_request`, but it is emitted by the Router before any agent runtime is invoked, not by a running agent graph — there is no checkpoint to resume; the user's answer starts the run against the chosen agent.

### `job_handoff`

```json
{"job_id": "job-1", "reason": "estimated_duration_exceeded", "poll_url": "/jobs/job-1"}
```

Sent when the plan-step duration estimate crosses the 10-minute async threshold (B23/B79) or when the agent is the Pipeline Manager (always async, B23). **No mid-run handoff (B79):** the async decision is made once, from the plan/triage step's own estimate, before the run emits any `component_add` for the answer surface. In practice `job_handoff` is therefore always one of the first events after `surface_begin` — it never arrives after content has already started streaming to the client; a run that has already begun streaming components runs to completion over this connection or ends in `error`, never switches to a background job mid-stream. The client stops listening to this stream and starts polling `GET /jobs/{jobId}` per `.agents/specs/api.md`. No `surface_end` follows on this connection; the eventual answer surface is delivered through chat history once the job completes, not through this stream.

### `heartbeat`

Not a named `event:` field — an SSE comment line (`:heartbeat\n\n`), sent every 15–20 seconds (B4) so CloudFront's per-packet origin timeout (default 30 s) never fires and so the client can distinguish "still working" from "connection died". It has no `id:` and does not consume a `seq` value.

### `surface_end`

```json
{"surface_id": "research-turn-42", "revision": 1, "root_id": "root", "final": true}
```

Terminal, normal-completion event. After this event, the client's replayed component graph is the final answer and is stored to Chat History (`.agents/specs/data-model.md`) exactly as a 1.1-shaped `ui` content block, with `version: "1.2"`.

### `error`

```json
{"message": "The request could not be completed.", "code": "internal_error"}
```

Reuses the exact redaction rule already implemented in `shared/transport/sse.py`: `message` is always the fixed safe string, and `code` is one of `invalid_request`, `rate_limited`, `service_unavailable`, `internal_error` (any other code collapses to `internal_error`). Terminal; no `surface_end` follows.

## Ordering rules (summary)

1. `surface_begin` is always first.
2. A component must exist (via `component_add`) before it is targeted by `text_delta`, `component_update`, or `grounding_update`, and before it is referenced as a `parent_id`.
3. `text_delta` events for one `component_id`/`field` apply strictly in the order sent.
4. The `pending` `grounding_update` for a component follows its `component_add`; the final-status `grounding_update` follows that component's last content-changing event.
5. `trace_event`, `progress`, and `usage` may interleave freely with component events and with each other; they never affect the component graph.
6. Exactly one of `surface_end`, `hitl_request`, `job_handoff`, or `error` terminates the stream, and it is always the last event.
7. `job_handoff`, when it occurs, precedes every `component_add` for the answer surface — the async-vs-streaming decision is made once, up front, from the plan step's estimate; there is no mid-run handoff after content has started streaming (B79).

## Idempotency and sequence numbers

Every non-heartbeat event's SSE `id:` field is the frame's `seq` (monotonic, starting at 0, scoped to this HTTP response only — not a global counter). The client tracks the highest `seq` it has applied. On receiving an event whose `seq` is less than or equal to the last applied `seq`, the client discards it as a duplicate (at-least-once delivery tolerance). `component_add` is additionally idempotent by `component_id`: re-adding the same `component_id` with identical fields is a no-op; re-adding it with different fields is treated as a `component_update`, logged as a protocol warning (this should not happen in a correct server implementation, but the client must not crash on it).

## Reconnect/resume behavior (B68)

Decided (B68 — replaces the earlier "Session Runner in-memory buffer" proposal): the Session Runner writes every non-heartbeat SSE event it emits to the Chat History Store as it sends it, keyed by `session_id` and the event's `seq` as `sequence_number` (`.agents/specs/data-model.md` "SSE event replay"). This durable log, not an in-memory buffer, is what a reconnect replays from.

- On a dropped connection, the client (using `fetch`/`ReadableStream`, which — unlike `EventSource` — has no built-in reconnect) detects the failure and re-opens `POST /chat/stream` carrying `resume_from_seq` set to the last applied `seq` (`.agents/specs/api.md`).
- The server queries the Chat History Store for this `session_id`'s SSE-replay items with `sequence_number` greater than `resume_from_seq`, replays them in order over the new connection, then continues live if the run is still in progress.
- If the run has already completed and produced a `surface_end`/`hitl_request`/`job_handoff`/`error`, replay ends at that event; there is nothing further to continue live.
- If no items exist for a `resume_from_seq` this high (for example, an invalid or stale value), the server returns `error` with `invalid_request`; the client falls back to `GET /sessions/{id}/turns/{messageId}/trace` and the history route (`.agents/specs/api.md`) per B59's lazy-load pattern.

The DynamoDB SK scheme for these SSE-replay items relative to `turn`/`trace_event` items is still a proposal (see `.agents/specs/data-model.md` TBD list); the resume mechanism itself is decided.

## Validation limits

All structural limits from `docs/cei-ui-protocol.md` ("Implemented Structural Limits") apply unchanged to the reconstructed surface (250 components, 32 graph depth, 12 table columns, etc.). Streaming-specific limits, proposed and not decided in the log:

| Limit | Proposed value |
|---|---|
| Events per stream | 500 (reuses the existing `MAX_SSE_EVENTS` constant) |
| `text_delta` size | 4 KiB per event (decided, B78) |
| Heartbeat interval | 15–20 s (this one **is** decided — B4) |
| Max stream duration | 15 minutes, 5 minutes idle (this one **is** decided — API Gateway REST streaming limits, per the research facts in the task brief) |

## Grounding pending state (B10)

Text streams token by token via `text_delta` with no grounding gate on prose. A groundable non-prose block (`Table`, `StatGrid`, `Graph`, or an `Artifact` with `data`) is added via `component_add` and immediately followed by a `grounding_update` with `status: "pending"` — this is the client-visible "verifying citations" state. Grounding itself still runs once, server-side, after the model call and before any of these events are emitted for that block (unchanged from `shared/grounding.py`); the "pending" window in the stream is a UI pacing device, not a sign that grounding is still computing when the block first appears. Proposal (not decided): exact timing — whether the server always inserts an artificial minimum pending duration for UX pacing, or emits `pending` and the final status back-to-back when grounding is already done.

## Worked example stream

```
id: 0
event: surface_begin
data: {"surface_id":"research-turn-42","revision":1,"content_type":"application/vnd.cei.ui+json","protocol":"cei-ui","version":"1.2","root_id":"root"}

id: 1
event: component_add
data: {"parent_id":null,"index":0,"component":{"id":"root","type":"Column","children":["s0-b0-text"]}}

id: 2
event: component_add
data: {"parent_id":"root","index":0,"component":{"id":"s0-b0-text","type":"Text","content":"","variant":"body"}}

id: 3
event: progress
data: {"status":"running","step_label":"Searching PubMed","current_step":1,"total_steps":3}

id: 4
event: trace_event
data: {"ts":"2026-09-29T00:00:01.000Z","step_type":"tool_call","tool_name":"pubmed___search","tool_input":{"query":"GLP-1 weight loss"},"tool_output_summary":"8 results","sources":["PMID:38291045"]}

id: 5
event: text_delta
data: {"component_id":"s0-b0-text","field":"content","delta":"Across eight trials, GLP-1 "}

id: 6
event: text_delta
data: {"component_id":"s0-b0-text","field":"content","delta":"agonists showed a consistent effect."}

: heartbeat

id: 7
event: component_add
data: {"parent_id":"root","index":1,"component":{"id":"s0-b1-statgrid","type":"StatGrid","metrics":[{"label":"Mean weight loss","value":"14.9","numeric_value":14.9,"unit":"%"}]}}

id: 8
event: grounding_update
data: {"component_id":"s0-b1-statgrid","status":"pending"}

id: 9
event: trace_event
data: {"ts":"2026-09-29T00:00:03.500Z","step_type":"reasoning","reasoning_text":"Cross-checked the 14.9% figure against two of the eight retrieved abstracts before including it.","reasoning_label":"summarized"}

id: 10
event: grounding_update
data: {"component_id":"s0-b1-statgrid","status":"verified","metrics":[{"index":0,"verified":true}]}

id: 11
event: usage
data: {"input_tokens":1820,"output_tokens":340,"total_tokens":2160,"estimated_cost_usd":0.0134}

id: 12
event: surface_end
data: {"surface_id":"research-turn-42","revision":1,"root_id":"root","final":true}
```

## TBD list (this file)

- TBD: whether an in-flight surface can ever legitimately restart at `revision > 1`.
- TBD: `usage` event emission cadence.
- TBD: whether `grounding_update`'s "pending" state has an artificial minimum duration for UX pacing.
- TBD: DynamoDB SK scheme for SSE-replay items relative to `turn`/`trace_event` items (cross-ref `.agents/specs/data-model.md`).

Resolved by B67–B86: `text_delta` maximum delta size is 4 KiB (B78); reconnect/resume request shape and route is `resume_from_seq` on `POST /chat/stream`, replaying from the Chat History Store (B68, cross-ref `.agents/specs/api.md`); the async threshold that triggers `job_handoff` is 10 minutes, decided once before streaming starts, with no mid-run handoff (B79); the Router's low-confidence question reuses the `ChoicePicker` shape but is not a `hitl_request` from a running graph (B80); this protocol also carries the tool-agent stream route unchanged (B84); the HITL wait has a decided expiry behavior, though the exact AgentCore Memory expiry number still needs verification (B82).
