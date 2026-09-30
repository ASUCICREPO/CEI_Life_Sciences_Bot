# CEI Life Sciences Bot — API

Status: **Planned — not built.** No API Gateway, Lambda route, or Cognito authorizer exists in this repository yet (`backend/lib/backend-stack.ts` defines only the AgentCore Code Interpreter). Everything in this document is the target design. Full detail: `.agents/specs/api.md`; the SSE event body carried on `/chat/stream` is defined in `.agents/specs/streaming-protocol.md`.

---

## Overview

One Regional REST API (API Gateway v1) serves every route. `/chat/stream` streams a chat turn as Server-Sent Events (CEI UI 1.2). Every other route is a normal buffered REST call. This design replaces an earlier plan for a separate HTTP API Gateway, because HTTP API (v2) cannot stream responses.

---

## Base URL

```
https://[api-id].execute-api.[region].amazonaws.com/[stage]/
```

**Planned.** The Region is configurable (no fixed Region); the endpoint is Regional (not edge-optimized) so it sits behind the project's own CloudFront distribution, because the edge-optimized endpoint's 30-second idle timeout is too short for streaming.

---

## Authentication

**Planned.** Every route requires a valid Amazon Cognito User Pools bearer token (`Authorization: Bearer <token>`), checked by one Cognito authorizer shared across all routes, including `/chat/stream`. There is no MFA. Each user only ever sees their own data (partitioned by Cognito `sub`).

---

## Streaming note (`/chat/stream`)

`/chat/stream` is the one route with `responseTransferMode: STREAM`. It returns `text/event-stream`, delivered over `fetch` + `ReadableStream` (not `EventSource`, because `EventSource` cannot send an `Authorization` header). A heartbeat comment is sent every 15–20 seconds. Limits: 15-minute maximum stream duration, 5-minute idle timeout, 2 MB/s after the first 10 MB. The event catalog (`surface_begin`, `component_add`, `text_delta`, `component_update`, `grounding_update`, `trace_event`, `progress`, `usage`, `hitl_request`, `job_handoff`, `surface_end`, `error`) is CEI UI 1.2 — full definition in `.agents/specs/streaming-protocol.md` and `docs/cei-ui-protocol.md`.

A run whose plan step estimates more than 10 minutes does not stream to completion; it emits `job_handoff` and the client switches to the Jobs routes below. This decision is made once, before any part of the answer streams — there is no mid-run handoff from a streaming run to a background job.

The Session Router can also return a `ChoicePicker` surface asking the user to pick one of the 4 agents, when a free-text request's classifier confidence is low. The user's pick then starts the run.

---

## Standard error envelope

**Planned.** Every non-2xx response uses this shape:

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

`code` is one of: `invalid_request`, `unauthorized`, `forbidden`, `not_found`, `rate_limited`, `concurrency_limit`, `stale_revision`, `service_unavailable`, `internal_error`. `message` is always a safe, redacted, user-facing string — never a stack trace, model ID, or secret.

| Code | When |
|---|---|
| `unauthorized` | Missing or invalid Cognito token |
| `forbidden` | A Cedar Policy deny, or a resource that belongs to another user |
| `not_found` | The session/job/file/export ID does not exist for this user |
| `rate_limited` | The account-wide API Gateway throttle (20 requests/second) |
| `concurrency_limit` | The user is already at their concurrent-run limit (2 real-time, 1 background) |
| `stale_revision` | A `/ui-actions` request targets a surface revision that is no longer current |
| `invalid_request` | The request body failed schema validation |
| `service_unavailable` / `internal_error` | An upstream failure (AgentCore, DynamoDB, etc.); message is always the redacted generic string |

---

## Pagination

**Planned.** List and history routes take `?limit=N&cursor=<opaque>`. `cursor` is an opaque, base64-encoded continuation token; the server never exposes raw DynamoDB keys.

```json
{
  "items": [ /* ... */ ],
  "next_cursor": "eyJ...",
  "has_more": true
}
```

Default `limit` is 20; maximum is 100. `next_cursor` is `null` and `has_more` is `false` on the last page.

---

## Route table

Every route requires the Cognito authorizer. All routes are **Planned — not built**.

| Method | Path | Mode | Purpose |
|---|---|---|---|
| POST | `/chat/stream` | STREAM | Send a chat message; streams the answer as CEI UI 1.2 SSE |
| POST | `/tools/{source}/chat/stream` | STREAM | Chat with exactly one data source (a "tool agent"); same event catalog as `/chat/stream` |
| POST | `/sessions` | BUFFERED | Create a chat session |
| GET | `/sessions` | BUFFERED | List the caller's sessions (paginated) |
| GET | `/sessions/{sessionId}` | BUFFERED | Get one session's metadata |
| PATCH | `/sessions/{sessionId}` | BUFFERED | Update session title and/or mode |
| DELETE | `/sessions/{sessionId}` | BUFFERED | Delete a session (starts the delete cascade, `202 Accepted`) |
| GET | `/sessions/{sessionId}/history` | BUFFERED | Cursor-paginated turn history (no trace) |
| GET | `/sessions/{sessionId}/turns/{messageId}/trace` | BUFFERED | Load one turn's trace log (lazy-loaded on thinking-block expand) |
| POST | `/ui-actions` | BUFFERED | Submit a typed CEI UI action (including HITL answers) |
| POST | `/sessions/{sessionId}/attachments/presign` | BUFFERED | Get a pre-signed S3 POST for a file upload (max 55 MB) |
| GET | `/sessions/{sessionId}/attachments/{fileId}` | BUFFERED | Get attachment processing status |
| DELETE | `/sessions/{sessionId}/attachments/{fileId}` | BUFFERED | Delete an attachment and its Knowledge Base chunks |
| GET | `/jobs` | BUFFERED | List the caller's background jobs |
| GET | `/jobs/{jobId}` | BUFFERED | Get one background job's status |
| POST | `/jobs/{jobId}/cancel` | BUFFERED | Cancel a running background job (`202 Accepted`) |
| GET | `/settings` | BUFFERED | Get user settings (default model, default mode, configured sources) |
| PUT | `/settings` | BUFFERED | Update user settings |
| GET | `/models` | BUFFERED | List selectable Bedrock models (blocklist already applied) |
| PUT | `/settings/api-keys/{sourceId}` | BUFFERED | Store a user-owned API key for one data source |
| DELETE | `/settings/api-keys/{sourceId}` | BUFFERED | Remove a stored API key |
| GET | `/exports/{exportId}/download` | BUFFERED | Get a pre-signed download URL for a completed export |
| POST | `/account/delete` | BUFFERED | Delete the account and all of its data (`202 Accepted`) |

There is no `POST /jobs` route — background jobs are always started internally by the Session Router or the Pipeline Manager, never created directly by a client.

Full request/response bodies for every route, the exact HITL action shape, and export format list (PDF, DOCX, Markdown, CSV, LaTeX, TXT): `.agents/specs/api.md`.

---

## Still to decide

- Whether the Router→Runtime token travels as an allowlisted custom header or as `Authorization`.
- The exact error shape returned to the agent (and then to the user) when a Cedar Policy deny happens mid-run.

Already decided since this page was first drafted: default/maximum pagination `limit` is 20/100; `/chat/stream` reconnect/resume uses `resume_from_seq` on the same route; chat/account delete responses are asynchronous, `202 Accepted`, with a Deletion Worker cascade; a client can request an ad hoc export of an existing answer (`POST /sessions/{id}/turns/{turn}/export`); and the async threshold is 10 minutes, decided once before streaming starts, with no mid-run handoff.

See `.agents/specs/api.md` for the full TBD list.
