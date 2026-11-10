# CEI Life Sciences Bot — API

Status: **Partly built.** `backend/lib/backend-stack.ts` defines the Regional REST API (stage `v1`), the Cognito authorizer, and the routes marked **Built** in the route table. The routes marked **Planned** do not exist yet. Full detail: `.agents/specs/api.md`; the SSE event body carried on `/chat/stream` is defined in `.agents/specs/streaming-protocol.md`.

---

## Overview

One Regional REST API (API Gateway v1) serves every route. `/chat/stream` streams a chat turn as Server-Sent Events (CEI UI 1.2). Every other route is a normal buffered REST call. This design replaces an earlier plan for a separate HTTP API Gateway, because HTTP API (v2) cannot stream responses.

---

## Base URL

```
https://[api-id].execute-api.[region].amazonaws.com/[stage]/
```

**Built.** The stage is `v1`. The SPA reads the base URL from `/config.json` (stack output `ApiBaseUrl`). CloudFront also sends `/chat/stream` to this API. The Region is configurable (no fixed Region); the endpoint is Regional (not edge-optimized) so it sits behind the project's own CloudFront distribution, because the edge-optimized endpoint's 30-second idle timeout is too short for streaming.

---

## Authentication

**Built.** Every public route requires a valid Amazon Cognito User Pools access token (`Authorization: Bearer <token>`, scope `openid`). One Cognito authorizer checks all public routes, including `/chat/stream`. The internal route `POST /internal/token-refresh` uses `AWS_IAM` auth, not Cognito. There is no MFA. Each user only ever sees their own data (partitioned by Cognito `sub`).

---

## Streaming note (`/chat/stream`)

`/chat/stream` is the one route with `responseTransferMode: STREAM`. It returns `text/event-stream`, delivered over `fetch` + `ReadableStream` (not `EventSource`, because `EventSource` cannot send an `Authorization` header). The Session Runner sends the SSE comment `: heartbeat` every 17 seconds. Limits: 15-minute maximum stream duration, 5-minute idle timeout, 2 MB/s after the first 10 MB. The event catalog (`surface_begin`, `component_add`, `text_delta`, `component_update`, `grounding_update`, `trace_event`, `progress`, `usage`, `hitl_request`, `job_handoff`, `surface_end`, `error`) is CEI UI 1.2 — full definition in `.agents/specs/streaming-protocol.md` and `docs/cei-ui-protocol.md`.

A run whose plan step estimates more than 10 minutes does not stream to completion; it emits `job_handoff` and the client switches to the Jobs routes below. This decision is made once, before any part of the answer streams — there is no mid-run handoff from a streaming run to a background job.

**Planned (F9), not built.** The Session Router can also return a `ChoicePicker` surface asking the user to pick one of the 4 agents, when a free-text request's classifier confidence is low. The user's pick then starts the run.

---

## Standard error envelope

**Built** (`backend/lambdas/shared/http_errors.py`). Every non-2xx response uses this shape:

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

**Built** (`backend/lambdas/shared/http_errors.py`). List and history routes take `?limit=N&cursor=<opaque>`. `cursor` is an opaque, base64-encoded continuation token; the server never exposes raw DynamoDB keys.

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

Every public route requires the Cognito authorizer. The Status column shows what exists in `backend/lib/backend-stack.ts` and `backend/lib/constructs/background-jobs.ts`.

| Method | Path | Mode | Purpose | Status |
|---|---|---|---|---|
| POST | `/chat/stream` | STREAM | Send a chat message; streams the answer as CEI UI 1.2 SSE | Built (SessionRunner) |
| POST | `/tools/{source}/chat/stream` | STREAM | Chat with exactly one data source (a "tool agent"); same event catalog as `/chat/stream` | Planned |
| POST | `/sessions` | BUFFERED | Create a chat session | Built |
| GET | `/sessions` | BUFFERED | List the caller's sessions (paginated) | Built |
| GET | `/sessions/{sessionId}` | BUFFERED | Get one session's metadata | Built |
| PATCH | `/sessions/{sessionId}` | BUFFERED | Update session title and/or mode | Built |
| DELETE | `/sessions/{sessionId}` | BUFFERED | Delete a session (starts the delete cascade, `202 Accepted`) | Planned (F19, deferred) |
| GET | `/sessions/{sessionId}/history` | BUFFERED | Cursor-paginated turn history (no trace) | Built |
| GET | `/sessions/{sessionId}/turns/{messageId}/trace` | BUFFERED | Load one turn's trace log (lazy-loaded on thinking-block expand) | Built |
| GET | `/sessions/{sessionId}/exports` | BUFFERED | List the exports of one session | Built |
| POST | `/ui-actions` | STREAM | Submit a typed CEI UI action (including HITL answers); streams the resumed run | Built (UiActions; only Analysis and Pipeline pause today) |
| POST | `/sessions/{sessionId}/attachments/presign` | BUFFERED | Get a pre-signed S3 POST for a file upload (max 55 MB) | Planned |
| GET | `/sessions/{sessionId}/attachments/{fileId}` | BUFFERED | Get attachment processing status | Planned |
| DELETE | `/sessions/{sessionId}/attachments/{fileId}` | BUFFERED | Delete an attachment and its Knowledge Base chunks | Planned |
| GET | `/jobs` | BUFFERED | List the caller's background jobs | Built |
| GET | `/jobs/{jobId}` | BUFFERED | Get one background job's status | Built |
| POST | `/jobs/{jobId}/cancel` | BUFFERED | Cancel a running background job (`202 Accepted`) | Built |
| GET | `/settings` | BUFFERED | Get user settings (default model, default mode, configured sources) | Planned |
| PUT | `/settings` | BUFFERED | Update user settings | Planned |
| GET | `/models` | BUFFERED | List selectable Bedrock models (blocklist already applied) | Planned |
| PUT | `/settings/api-keys/{sourceId}` | BUFFERED | Store a user-owned API key for one data source | Planned |
| DELETE | `/settings/api-keys/{sourceId}` | BUFFERED | Remove a stored API key | Planned |
| GET | `/exports/{exportId}/download` | BUFFERED | Get a pre-signed download URL for a completed export | Built |
| POST | `/client-diagnostics` | BUFFERED | Record a bounded browser failure counter (no user content) | Built |
| POST | `/account/delete` | BUFFERED | Delete the account and all of its data (`202 Accepted`) | Planned (F19, deferred) |

The internal route `POST /internal/token-refresh` (`backend/lib/constructs/tool-gateway.ts`) uses `AWS_IAM` auth. It is not a client route. Only the Research runtime role can call it, to refresh the tool-scoped token of a run (B67).

There is no `POST /jobs` route — background jobs are always started internally by the Session Router or the Pipeline Manager, never created directly by a client.

Full request/response bodies for every route, the exact HITL action shape, and export format list (PDF, DOCX, Markdown, CSV, LaTeX, TXT): `.agents/specs/api.md`.

---

## Still to decide

- The exact error shape returned to the agent (and then to the user) when a Cedar Policy deny happens mid-run.

Already decided since this page was first drafted: the Router-to-runtime tool token travels in the custom header `X-Cei-Gateway-Token` (`backend/lambdas/session-runner/index.js`), and `Authorization` keeps the user's own token; default/maximum pagination `limit` is 20/100; `/chat/stream` reconnect/resume uses `resume_from_seq` on the same route; chat/account delete responses are asynchronous, `202 Accepted`, with a Deletion Worker cascade; a client can request an ad hoc export of an existing answer (`POST /sessions/{id}/turns/{turn}/export`, planned, not built); and the async threshold is 10 minutes, decided once before streaming starts, with no mid-run handoff.

See `.agents/specs/api.md` for the full TBD list.
