# API Spec

Status: planned. Source of truth is `.agents/decision-log.md` (B1–B86). Where this spec conflicts with `docs/APIDoc.md` (a placeholder template) or `docs/architectureDeepDive.md`, the decision log wins.

Related specs (not owned here): `.agents/specs/architecture.md`, `.agents/specs/agents.md`, `.agents/specs/tools.md`, `.agents/specs/auth-and-tool-scope.md` (token scopes for the `/chat/stream` → Gateway path), `.agents/specs/data-model.md` (backing tables), `.agents/specs/streaming-protocol.md` (the SSE event body carried on `/chat/stream`).

## Shape of the API (B2)

One Regional REST API (API Gateway v1), not HTTP API v2 (which cannot stream). One Cognito User Pools authorizer on every client-facing route. `/chat/stream` uses `responseTransferMode: STREAM`; every other client-facing route is `BUFFERED`. The one exception is the internal `/internal/token-refresh` route (B67), which uses an `AWS_IAM` authorizer restricted to the runtime execution role, not the Cognito authorizer — see "Internal (service-to-service)" below. Regional endpoint sits behind our own CloudFront (the edge-optimized endpoint's 30 s idle timeout is too short for streaming). Throttle: 20 requests/second per account-wide API Gateway setting (B22); per-user concurrency is enforced in Lambda against the Active-run counters (see `.agents/specs/data-model.md`), not by API Gateway. Phase: F3 (API + authorizer + throttling foundation), F5 (streaming route live).

## Standard error envelope

Every non-2xx response uses this shape:

```json
{
  "error": {
    "code": "invalid_request",
    "message": "The session_id field is required.",
    "request_id": "req_018f...",
    "retriable": false
  }
}
```

`code` is one of: `invalid_request`, `unauthorized`, `forbidden`, `not_found`, `rate_limited`, `concurrency_limit`, `stale_revision`, `service_unavailable`, `internal_error`. This deliberately reuses the four codes already defined for SSE errors in `shared/transport/sse.py` (`invalid_request`, `rate_limited`, `service_unavailable`, `internal_error`) and adds REST-only codes. `message` is a safe, redacted, user-facing string — never a stack trace, model ID, or secret (B7's redaction rule applies to every error surface, not only the trace).

## Pagination (cursor, B59)

List and history routes take `?limit=N&cursor=<opaque>`. `cursor` is a base64 encoding of the DynamoDB `LastEvaluatedKey`; the server never exposes raw DynamoDB keys. Response envelope:

```json
{
  "items": [ ... ],
  "next_cursor": "eyJ..." ,
  "has_more": true
}
```

`next_cursor` is `null` and `has_more` is `false` on the last page. Default `limit` is 20; maximum is 100 (decided, B78).

## Route list

Every route requires the Cognito authorizer unless noted. `Phase` marks when the route is expected to exist; a route existing does not imply every field below is final before that phase lands.

### Chat / streaming

| Method | Path | Lambda | Mode | Phase |
|---|---|---|---|---|
| POST | `/chat/stream` | Session Runner (Node.js, B3) | STREAM | S1, F5 |
| POST | `/tools/{source}/chat/stream` | Session Runner (Node.js, B3) | STREAM | F15 |

Request:

```json
{
  "session_id": "session-123",
  "message": "Summarize the evidence for...",
  "attachment_ids": ["file-1"]
}
```

Response: `text/event-stream` body. Event framing and payloads are defined in `.agents/specs/streaming-protocol.md` (CEI UI 1.2). The Session Runner relays `InvokeAgentRuntime` chunks from the Session Router → AgentCore Runtime path; it does not itself decide the agent (B17).

A run whose plan step estimates more than 10 minutes (B79 — resolves the B23 threshold TBD) does not stream to completion. This is decided once, from the plan step's own estimate, before any component streaming begins; there is no mid-run handoff to async (B79). It emits a `job_handoff` event (see streaming spec) and the client switches to the Jobs routes below.

When the free-text classifier's confidence is low, the Session Router does not call `/chat/stream` against a chosen agent at all; it instead returns a `ChoicePicker` surface asking the user which of the 4 agents they want (B80). The user's answer (via `POST /ui-actions`) is what actually starts the run against the chosen agent.

**Reconnect/resume (B68 — resolves the earlier resume-route proposal):** the same route, `POST /chat/stream`, takes an optional `resume_from_seq` field instead of a separate route:

```json
{ "session_id": "session-123", "resume_from_seq": 12 }
```

The Session Runner queries the Chat History Store for SSE-replay items (`.agents/specs/data-model.md`) with `sequence_number` greater than `resume_from_seq` for this `session_id`, replays them over the new connection, then continues live. See `.agents/specs/streaming-protocol.md` "Reconnect/resume behavior".

**Tool agent chat (B84):** `POST /tools/{source}/chat/stream` is a separate STREAM route for single-source tool-agent chat, handled by the same Session Runner Lambda and the same request/response/reconnect shapes as `/chat/stream` above. The `{source}` path parameter (one of the 10 sources or `ncbi`) tells the Session Router which YAML profile to load and which single source scope to request for the token (see `.agents/specs/auth-and-tool-scope.md` §3, `.agents/specs/agents.md` §5); the Router checks `source` against the allowed source list before dispatch.

### Internal (service-to-service, IAM auth, B67)

| Method | Path | Lambda | Auth | Phase |
|---|---|---|---|---|
| POST | `/internal/token-refresh` | Token Refresh Lambda | AWS_IAM (not Cognito) | F4 |

Request/response:

```json
// request (from the calling AgentCore runtime execution role)
{ "session_id": "session-123", "job_id": "job-1" }

// response
{ "access_token": "...", "expires_in": 3600, "scopes": ["cei-tools/src.pubmed", "cei-tools/common"] }
```

Called by a running agent (real-time or async) before its 1 h tool token expires. The endpoint checks the Job ID Store (`job_id` present) or the Active-run counters record (real-time run, no `job_id`) to confirm the run is still active, then requests a new client-credentials token from Cognito with the same scopes and user `sub`/session ID (see `.agents/specs/auth-and-tool-scope.md` §5). Not reachable through CloudFront/the Cognito authorizer; restricted to the runtime execution role via the API Gateway resource policy.

### Sessions (Session Metadata Handler)

| Method | Path | Mode | Phase |
|---|---|---|---|
| POST | `/sessions` | BUFFERED | F3 |
| GET | `/sessions` | BUFFERED | F3 |
| GET | `/sessions/{sessionId}` | BUFFERED | F3 |
| PATCH | `/sessions/{sessionId}` | BUFFERED | F3 |
| DELETE | `/sessions/{sessionId}` | BUFFERED | F19 |

`POST /sessions` request/response:

```json
// request
{ "title": "New chat", "mode": "science", "model_id": "us.anthropic.claude-..." }

// response 201
{ "session_id": "session-123", "title": "New chat", "mode": "science", "status": "active", "created_at": "2026-09-29T00:00:00Z" }
```

`GET /sessions` — paginated list, cursor as above. `GET /sessions/{sessionId}` — one session's metadata. `PATCH /sessions/{sessionId}` — partial update of `title` and/or `mode` (B55: mode is set per chat and can change mid-chat). `DELETE /sessions/{sessionId}` — marks the session "deleting" and returns `202 Accepted` at once; the Deletion Worker Lambda then runs the chat-delete cascade defined in `.agents/specs/data-model.md` (B58, B73). The UI hides the session immediately on the `202` response.

### Chat history (Session History Handler)

| Method | Path | Mode | Phase |
|---|---|---|---|
| GET | `/sessions/{sessionId}/history` | BUFFERED | F3 |
| GET | `/sessions/{sessionId}/turns/{messageId}/trace` | BUFFERED | F6 |

`GET .../history?limit=20&cursor=...` returns turns only (no trace), newest page first, oldest-on-scroll-up (B59):

```json
{
  "items": [
    {
      "message_id": "msg-1",
      "role": "ai",
      "content_blocks": [{"block_type": "text", "body": "..."}],
      "citations": [],
      "timestamp": "2026-09-29T00:00:01Z",
      "sequence_number": 42
    }
  ],
  "next_cursor": "eyJ...",
  "has_more": true
}
```

`ui` content blocks validate under CEI UI 1.0, 1.1, or 1.2 (see `.agents/specs/streaming-protocol.md` "Version negotiation"); the client falls back to `text` on validation failure (existing rule, `docs/cei-ui-protocol.md` "Text Fallback").

`GET .../turns/{messageId}/trace` loads the trace only when the user expands that turn's thinking block (B59, B7). Returns the ordered trace-event log for that turn (steps, tool calls with inputs/outputs, sources, grounding decisions, reasoning text labeled `"summarized"` where applicable) — never model IDs, guardrail/system prompts, prompt versions, or secrets (B7).

### HITL actions

| Method | Path | Mode | Phase |
|---|---|---|---|
| POST | `/ui-actions` | BUFFERED | F13 |

Request/response reuse the existing `ActionRequest`/`ActionResponse` shapes from `backend/agents/shared/schemas/cei_ui.py` (`request_id`, `session_id`, `surface_id`, `revision`, `interaction_id`, `component_id`, `action_id`, `value`; response adds `status` ∈ `accepted|completed|stale|rejected|error` and optional `message`). This is the production version of today's local `frontend/app/api/ui-actions/route.ts` preview proxy. Per B56, a `completed` response for a HITL action resumes the paused LangGraph run from its checkpoint (`AgentCoreMemorySaver`, B28) in a new run; it does not reopen the original SSE connection. The client is expected to re-open `/chat/stream` (or receive the resumed answer via the Jobs routes if the resumed run goes async) to see the continuation. Exact resumption delivery channel is a cross-reference to `.agents/specs/streaming-protocol.md`; not fully decided.

### Attachments

| Method | Path | Lambda | Mode | Phase |
|---|---|---|---|---|
| POST | `/sessions/{sessionId}/attachments/presign` | Attachment Upload Worker | BUFFERED | F10 |
| GET | `/sessions/{sessionId}/attachments/{fileId}` | — | BUFFERED | F10 |
| DELETE | `/sessions/{sessionId}/attachments/{fileId}` | — | BUFFERED | F10/F19 |

`POST .../presign` request/response:

```json
// request
{ "filename": "report.pdf", "content_type": "application/pdf", "byte_size": 2_000_000 }

// response
{
  "file_id": "file-1",
  "upload_url": "https://...s3.amazonaws.com/...",
  "fields": { "key": "...", "policy": "...", "x-amz-signature": "..." },
  "max_byte_size": 55000000
}
```

Presigned POST with a `content-length-range` condition capping the upload at 55 MB (B32). Allowed `content_type` values map to B32's file type list (PDF, DOCX, TXT, MD, CSV, PNG, JPG). `GET .../{fileId}` returns processing `status` (`uploading|processing|ready|failed`). `DELETE .../{fileId}` removes the S3 object, the metadata row, and any KB chunks for that file (B58).

### Jobs (Polling Lambda)

| Method | Path | Mode | Phase |
|---|---|---|---|
| GET | `/jobs` | BUFFERED | F16 |
| GET | `/jobs/{jobId}` | BUFFERED | F16 |
| POST | `/jobs/{jobId}/cancel` | BUFFERED | F16 |

There is no `POST /jobs` route. Jobs are started internally by the Session Router or the Pipeline Manager when a run is predicted (B23) or always (Pipeline Manager, B23) to exceed 15 minutes; a client never creates a job directly.

```json
// GET /jobs/{jobId}
{
  "job_id": "job-1",
  "session_id": "session-123",
  "agent_type": "pipeline_manager",
  "status": "running",
  "progress_pct": 40,
  "updated_at": "2026-09-29T00:10:00Z"
}
```

`POST /jobs/{jobId}/cancel` sets the cooperative `cancel_requested` flag and calls `StopRuntimeSession` (B25); response `202 Accepted` with `status: "cancelling"`. The `/pipeline` view (B54) polls `GET /jobs/{jobId}` for live step status; it has no separate route.

### Settings (Fetch User Settings)

| Method | Path | Mode | Phase |
|---|---|---|---|
| GET | `/settings` | BUFFERED | F3 |
| PUT | `/settings` | BUFFERED | F3 |
| GET | `/models` | BUFFERED | F8 |
| PUT | `/settings/api-keys/{sourceId}` | BUFFERED | F18 |
| DELETE | `/settings/api-keys/{sourceId}` | BUFFERED | F18 |

`GET/PUT /settings` — `default_model_id`, `default_mode`, `configured_sources` (list of source IDs, values only, never key material — B18/B20). `GET /models` — Bedrock model list filtered by the configurable blocklist env var (B18); response is `{"models": [{"model_id": "...", "label": "..."}]}`.

`PUT /settings/api-keys/{sourceId}` — request `{"api_key": "..."}`, response `{"source_id": "ncbi", "configured": true}` — **the key value is never returned by any route**, including this one's own response. `DELETE /settings/api-keys/{sourceId}` removes the Secrets Manager secret.

### Exports

| Method | Path | Mode | Phase |
|---|---|---|---|
| POST | `/sessions/{sessionId}/turns/{turn}/export` | BUFFERED | F7 |
| GET | `/exports/{exportId}/download` | BUFFERED | F7 |

`POST .../turns/{turn}/export` (B74 — resolves the earlier ad-hoc-export proposal) — a user-triggered export of an already-completed answer, in addition to exports an agent produces during a run. Request carries the D11/B81 review acknowledgment (the export dialog's "I have reviewed this output" checkbox; the Export button is disabled until it is checked, and the acknowledgment is logged):

```json
// request
{ "format": "pdf", "reviewed": true }

// response 202
{ "export_id": "export-1", "status": "processing" }
```

`GET /exports/{exportId}/download` returns `{"download_url": "https://...", "expires_at": "..."}` — a presigned S3 GET against the export outputs bucket (`.agents/specs/data-model.md`), URL TTL 15 minutes (B78). Formats are PDF, DOCX, MD, CSV, LaTeX, TXT (B41, B61 keeps `txt`). Export generation is either agent-invoked (Drafting agent tool, `shared/tools/export.py`) or user-triggered via the POST route above (B74).

### Account

| Method | Path | Mode | Phase |
|---|---|---|---|
| POST | `/account/delete` | BUFFERED | F19 |

Marks the account "deleting" and returns `202 Accepted` at once; the Deletion Worker Lambda then runs the account-delete cascade (B58, B73, `.agents/specs/data-model.md`).

## Errors table (per-route summary)

| Code | When |
|---|---|
| `unauthorized` | Missing/invalid Cognito token |
| `forbidden` | Cedar Policy deny, or a resource that exists but belongs to another user (B12) |
| `not_found` | `session_id`/`job_id`/`file_id`/`export_id` does not exist for this user |
| `rate_limited` | API Gateway 20 rps throttle (B22) |
| `concurrency_limit` | Active-run counters at the B22 limit (2 real-time, 1 background) |
| `stale_revision` | A `/ui-actions` request against a surface `revision` that is no longer current (reuses the existing `stale` action-response status) |
| `invalid_request` | Schema validation failure on the request body |
| `service_unavailable` / `internal_error` | Upstream (AgentCore, DynamoDB, etc.) failure; message is always the redacted generic string, per B7 |

## TBD list (this file)

- TBD: whether the Router→Runtime token (and the refreshed token from `/internal/token-refresh`) travels as an allowlisted custom header or as `Authorization` — cross-ref `.agents/specs/auth-and-tool-scope.md` §5.
- TBD: denied-call error shape inside LangGraph when the Gateway returns a Cedar deny (cross-ref `.agents/specs/auth-and-tool-scope.md` §8).

Resolved by B67–B86: default/max pagination limit 20/100 (B78); `/chat/stream` reconnect/resume via `resume_from_seq` and Chat History Store replay (B68); the internal token-refresh route (B67); chat/account delete responses are `202 Accepted` with a Deletion Worker cascade (B73); the client-initiated export route `POST /sessions/{id}/turns/{turn}/export` (B74); export download URL TTL 15 min (B78); async threshold is 10 minutes with no mid-run handoff (B79); low-confidence routing uses a `ChoicePicker` of the 4 agents (B80); the tool-agent stream route `/tools/{source}/chat/stream` (B84).
