# Data Model Spec

Status: planned. Source of truth is `.agents/decision-log.md` (decisions B1–B86). Where this spec conflicts with `docs/architectureDeepDive.md`, `docs/APIDoc.md`, or `docs/agent-architecture-decisions.md`, the decision log wins.

Related specs (written in parallel, not owned by this file): `.agents/specs/architecture.md`, `.agents/specs/agents.md`, `.agents/specs/tools.md`, `.agents/specs/auth-and-tool-scope.md`.

Existing code this spec extends: `backend/agents/shared/persistence/chat_history.py`, `backend/agents/shared/persistence/chat_metadata.py`, `backend/agents/shared/memory/agentcore.py`, `backend/agents/shared/transport/async_task.py`, `backend/agents/shared/config.py`.

## Conventions (all tables)

- **Partitioning** — every table is partitioned so one user's data is never in the same partition key value as another user's, using the Cognito `sub` as `user_id` (B12). Phase: F3.
- **Encryption** — AWS-managed keys (SSE-S3 / AWS-owned KMS default) for DynamoDB, S3, and Secrets Manager. No customer-managed KMS key (B52). cdk-nag suppressions for encryption checks need an ADR-format reason string.
- **TTL** — no table has an item-level TTL by default. Retention is indefinite until account deletion (B8, B58, B104). There is no per-chat delete (B104). The active-run counters (below) are the one exception, and TBD whether they use DynamoDB TTL or explicit decrement.
- **Deletion cascade** (B58, B104, B109) — account delete only; see "Deletion cascade" section at the end. Status: planned, F19.

---

## 1. Session Metadata Store (`ChatMetadataStore`)

Exists (partial): `shared/persistence/chat_metadata.py` defines `ChatMetadataRecord` and a repository with `create` and `list_by_user` implemented; `get`, `update_title`, `increment_message_count`, `archive` raise `NotImplementedError`. Phase: F3 (create/get/list), F19 (account-delete cascade participant; no per-chat delete, B104).

| | |
|---|---|
| PK | `user_id` (Cognito `sub`) |
| SK | `session_id` |
| GSI-1 | `GSI1PK = status`, `GSI1SK = updated_at` — Proposal (not decided): needed only if the product ever lists sessions across users (for example an admin view); not required by any current access pattern, so omit until a phase needs it. |

Attributes (from `ChatMetadataRecord`): `session_id`, `user_id`, `title`, `project` (nullable), `status` (`active`/`archived`), `agent_type` (nullable), `model_id` (nullable — the user's selected Bedrock model, B18), `message_count`, `created_at`, `updated_at`. Add (planned, not yet in code): `mode` (`science`/`business`, per chat, B55).

Access patterns:
- Create a session (F3).
- List a user's sessions, optionally filtered by status (F3) — implemented via `Query` on PK `user_id`.
- Get one session by `user_id` + `session_id` (F3) — implemented today as `NotImplementedError`; needs a `get_item`.
- Update title (F3), increment message count on each turn (F3), archive (F3).
- Delete session record as part of the account-delete cascade (F19, B58, B104). There is no per-chat delete.

---

## 2. Chat History Store (`ChatHistoryStore`)

Exists (partial): `shared/persistence/chat_history.py` defines `ChatHistoryRecord` and a repository with `append` and `load_page` implemented; `delete_session` raises `NotImplementedError`. Phase: F3 (append/load), F6 (trace storage extensions), F19 (delete).

| | |
|---|---|
| PK | `session_id` |
| SK | `sequence_number` (integer, monotonic per session) |
| GSI-1 | Proposal (not decided): a `user_id` GSI is not in the code today. It is needed only if account deletion must scan chat history directly by user rather than by first listing the user's sessions from the Session Metadata Store and deleting each session's history by PK. This spec assumes the latter (no GSI needed) — see "Deletion cascade" below. |

Attributes (from `ChatHistoryRecord`): `session_id`, `message_id`, `role` (`human`/`ai`/`tool`), `content_blocks` (list of `{block_type: text|ui|a2ui|thinking, body}}`), `tool_name`, `tool_input`, `tool_output`, `citations`, `timestamp`, `sequence_number`.

One item per turn today. B7/B9 require the trace (the ordered log of graph steps, tool calls, sources, grounding decisions, and redacted reasoning — see `.agents/specs/streaming-protocol.md`) to also live in this raw store, one item per trace event, not folded into the turn item. Planned schema addition (F6):

- New `record_type` attribute: `"turn"` (today's shape), `"trace_event"`, or `"sse_event"` (B68 — see "SSE event replay" below).
- Trace-event items use the same PK (`session_id`) and a `sequence_number` interleaved with turn items so `load_page` returns both in time order, or a separate SK namespace (for example `SK = "trace#<turn_sequence>#<trace_seq>"`) so a turn's trace can be fetched only when its thinking block is expanded (B59, lazy load). Proposal (not decided): exact SK scheme; the decision log fixes the requirement (lazy load per turn) but not the key shape.

**SSE event replay (B68).** The Session Runner writes each SSE event (see `.agents/specs/streaming-protocol.md` event catalog) it emits to this store with its `seq` as the item's `sequence_number`, PK `session_id`. On a dropped connection, the client reconnects with its last applied `seq`; the server queries this store for items with a greater `sequence_number` for the same `session_id`, replays them, then continues live. This replaces the earlier "Runner in-memory buffer" proposal — B68 is decided; the exact SK/record_type layout for `sse_event` items relative to `trace_event`/`turn` items is still a proposal (see the SK-scheme TBD below).

**Large trace entries (B9/B78).** A trace payload over the 400 KB DynamoDB item limit is gzip-compressed and split into multiple items; no S3 overflow. Chunk size is decided: 350 KB of gzip output per chunk (B78, leaves headroom under 400 KB for key attributes and DynamoDB's own item overhead). Key schema is still a proposal, not decided:
- Key schema: propose `SK = "trace#<turn_sequence>#<trace_seq>#chunk#<n>"` with a sibling `#meta` item holding `{total_chunks, total_bytes, compressed: true}`; reads reassemble chunks in `n` order and gunzip the concatenated bytes.

Access patterns: append a turn, trace event, or SSE event (F3/F6); load a page of turns, newest-first then reversed to chronological, with `before_sequence` as the cursor (F3, matches B59's "cursor pages... with the DynamoDB `LastEvaluatedKey`" — see `.agents/specs/api.md` for the wire-level cursor); load one turn's trace on expand (F6); replay SSE events from a sequence number on reconnect (B68); delete all items for a session (F19).

---

## 3. Job ID Store (`JobIdStore`)

Exists (partial): `shared/transport/async_task.py` defines `TaskResult`/`TaskStatus` and an `AsyncTaskClient` whose `submit`/`poll`/`cancel` all raise `NotImplementedError`. This code predates B25 (Fargate removed; async jobs run as AgentCore Runtime async sessions). Phase: F16.

| | |
|---|---|
| PK | `user_id` |
| SK | `job_id` |
| GSI-1 | `GSI1PK = status`, `GSI1SK = updated_at` — for the watchdog to scan stale/running jobs without a full table scan. Phase F16. |

Attributes (planned, replacing the Fargate-era `TaskResult` shape):

| Attribute | Type | Notes |
|---|---|---|
| `job_id` | string | |
| `user_id` | string | Cognito `sub`. |
| `session_id` | string | The chat this job belongs to. |
| `agent_type` | string | Which specialist agent (Research/Drafting/Analysis/Pipeline Manager). |
| `status` | string | `pending`, `running`, `awaiting_approval` (HITL), `completed`, `partial`, `failed`, `cancelled`. |
| `progress_pct` | integer | 0–100. |
| `heartbeat_at` | timestamp | Updated by the running AgentCore async session (`HealthyBusy` ping, B25). The watchdog marks a job stale/failed when this is too old. |
| `cancel_requested` | boolean | Cooperative cancel flag (B25); the runtime polls this and calls `StopRuntimeSession` when set. |
| `checkpoint_ref` | string | Reference into the AgentCore Memory short-term store (`AgentCoreMemorySaver`, B28) for watchdog resume after a kill. |
| `runtime_session_id` | string | The AgentCore Runtime async session identifier, for `StopRuntimeSession`. |
| `created_at` / `updated_at` | timestamp | |
| `error` | string, nullable | Redacted per B7 (no model IDs, prompt text, or secrets). |

Access patterns: create on job start (internal only — see `.agents/specs/api.md`); update heartbeat/progress from the running session; get by `job_id` for polling; list a user's jobs; watchdog scan by `status = running` with a stale `heartbeat_at` (F16); set `cancel_requested` (F16); read by the Token Refresh Lambda (B67) to confirm a background job is still active before minting a new scoped tool token — see `.agents/specs/auth-and-tool-scope.md` §5.

For real-time (non-job) runs, the same "still active" check reads the per-user Active-run counters item (§6) instead of this table (B67).

---

## 4. User Metadata Table (`UserMetadataTable`)

Exists (partial): referenced only by `config.user_metadata_table`; no repository code yet. Phase: F3 (settings), F8 (model prefs/blocklist interaction), F18 (API key references).

| | |
|---|---|
| PK | `user_id` (Cognito `sub`) |
| SK | fixed value, e.g. `"profile"` — single-item-per-user table; no natural range key. |

Attributes (planned): `default_model_id`, `default_mode` (`science`/`business`, B55 default before per-chat override), `email` (for the domain allowlist check, B16 — Proposal (not decided): whether this is duplicated from Cognito or read live from the Cognito user pool), `configured_sources` (list of source IDs the user has added a key for, so the UI can show which optional keys exist without reading Secrets Manager — B20). No `blocked_model_overrides` attribute: there is no per-user model restriction beyond the global blocklist env var (B76 — resolves the earlier open question). `created_at` / `updated_at`.

Access patterns: get/put settings (F3); read `default_model_id`/`default_mode` when starting a session; list `configured_sources` for the settings page (F18).

---

## 5. Attachment Metadata Store (`AttachmentMetadataStore`)

Exists (partial): referenced only by `config.attachment_metadata_table`; no repository code yet. Phase: F10.

| | |
|---|---|
| PK | `session_id` (attachments are per chat, B31) |
| SK | `file_id` |
| GSI-1 | `GSI1PK = user_id`, `GSI1SK = created_at` — needed for account-delete cascade to find every attachment across all of a user's sessions in one query (B58) instead of iterating every session first. Phase F19. |

Attributes (planned): `file_id`, `session_id`, `user_id`, `filename`, `content_type`, `byte_size` (≤ 55 MB, B32), `s3_key` (Attachments Bucket), `status` (`uploading`, `processing`, `ready`, `failed`), `kb_ingested` (boolean — whether KB chunks exist for this file, B35), `created_at`.

Access patterns: create on presign (F10); update status from the Attachment Processor (F10); list a session's attachments (F10); list a user's attachments for account delete (F19, via GSI-1); delete one attachment's row plus its S3 object and KB chunks (F19, B58).

---

## 6. Active-run counters (concurrency limits, B22)

Not yet in code. Phase: F5 (real-time limit), F16 (background limit).

| | |
|---|---|
| Table | Proposal (not decided): a new small table `ActiveRunCounters`, or an attribute set on the User Metadata Table item. This spec proposes a separate table so high-frequency counter writes do not contend with the low-frequency settings item. |
| PK | `user_id` |
| SK | fixed value, e.g. `"counters"` |

Attributes (planned): `active_realtime_runs` (integer, max 2 per B22), `active_background_runs` (integer, max 1 per B22). Incremented with a conditional `UpdateItem` (`ADD ... value` guarded by `active_realtime_runs < 2`) when a run starts, decremented when it ends or the watchdog marks it failed/stale. Proposal (not decided): exact recovery path if a Lambda crashes between increment and the matching decrement (a leaked counter that never frees a slot). Candidates: a TTL-based counter-lease pattern, or the F16 watchdog also repairs counters when it marks a job stale. Not decided.

---

## 7. S3 Buckets

| Bucket | Purpose | Key facts | Phase |
|---|---|---|---|
| Static site bucket | Hosts the built frontend | CloudFront origin, private, OAC | F2 |
| Attachments bucket | User uploads | Presigned POST with a `content-length-range` condition capping uploads at 55 MB (B32); key scheme proposal: `{user_id}/{session_id}/{file_id}/{filename}`; SQS-triggered processing (existing `AttachmentProcessor` role in `docs/architectureDeepDive.md`) | F10 |
| KB source / S3 Vectors bucket | Bedrock Knowledge Base source documents and the S3 Vectors index (B33) | Every chunk carries `user_id`, `session_id`, `file_id` metadata; every retrieval filters on this metadata | S5, F10 |
| Export outputs bucket | Holds generated PDF/DOCX/MD/CSV/LaTeX/TXT files (B41) produced either by an agent during a run or by a user-triggered request (B74) | B101: S3 Intelligent-Tiering with no archive tier or automatic expiry. Export metadata is keyed by user and export ID, with a session-prefixed ID allowing a chat query without scanning S3. A 15-minute presigned GET is issued only on a user download click (B78/B101). Delete chat/account removes its report objects and records. Bucket name remains deployment-generated. | F7 |

---

## 8. Secrets Manager — user API keys

Exists (partial): `config.api_key_secret_prefix = "cei/user-keys"` is the only current reference.

Decided (B78): secret name `cei/user-keys/{user_id}/{source_id}` (one secret per user per configured source, e.g. NCBI), so Gateway Lambdas can read a user's key with a scoped IAM policy keyed on `user_id` (see `.agents/specs/auth-and-tool-scope.md` for how the `sub` claim reaches the Gateway Lambda). Never returned by any GET route (see `.agents/specs/api.md`). Phase: F18.

---

## 9. AgentCore Memory

| Memory type | Backing | Namespace | Phase |
|---|---|---|---|
| Short-term (checkpointer) | `AgentCoreMemorySaver` (B28), code exists in `shared/memory/agentcore.py` (`create_checkpointer`, `build_invocation_config`) but is not wired into any graph yet | `thread_id = session_id`, `actor_id = user_id` | F5 (wire-up), S3/S4 (async and HITL resume) |
| Long-term — `user_preferences` strategy | AgentCore Memory built-in strategy (B37) | Per-user namespace (`actor_id = user_id`), shared across all of a user's chats | F11 |
| Long-term — `semantic_facts` strategy | AgentCore Memory built-in strategy (B37) | Per-user namespace (`actor_id = user_id`) | F11 |

No explicit "remember" tool; corrections are extracted by these two strategies only (B37). Users cannot see or edit long-term memories in the MVP (B36). AgentCore Memory short-term event expiry (B82 — refines B57): the checkpointer uses the maximum available event expiry (expected around 365 days; the exact number still needs verification against AWS documentation). A HITL pause that is still waiting when its checkpointed event expires shows "expired, start again" instead of resuming — the user must restart the task. This is a decided behavior, not an unlimited wait; only the exact expiry number is still to be confirmed.

---

## 10. Web Search storage rule (B44)

Not a table of its own. Wherever tool-call output from the Web Search Gateway connector is persisted (Chat History trace, per item 2 above), store only the URL, title, and the specific snippet the answer cited — never the full result list. This is a data-model constraint on trace-event content, not a separate store, and it exists because the AgentCore Web Search connector's terms forbid bulk storage of results.

---

## Deletion cascade (B58/B73/B104/B109)

There is no chat delete (B104): `DELETE /sessions/{sessionId}` is removed. Only `POST /account/delete` exists, and it is account-wide.

Decided (B109): `POST /account/delete` is behind a flag, off by default. It disables the Cognito user (`AdminDisableUser`), revokes every session (`AdminUserGlobalSignOut`), and writes an account "deleting" gate that every run-starting route checks first, so no new run can start. It returns `202 Accepted` at once; the UI hides the account's data immediately. A running AgentCore session ends on its own at the 8-hour maximum. A scheduled worker then runs the cascade below, 9 hours after the request (8-hour run maximum plus 1-hour access-token life) — by then, a late write from a timed-out SDK retry has already finished, so no epoch-conditional write is needed. Account data can stay for up to about 9 hours after the request. Whether the worker runs synchronously or is fanned out (step function / SQS) for large accounts is still a proposal, not decided.

**Account delete** removes, for every session the user has: Chat History Store items (turns + trace + SSE-replay items, including chunked trace items), Attachment Metadata Store rows plus their S3 objects, KB chunks filtered by `session_id` (S3 Vectors + Bedrock KB), and the AgentCore Memory short-term checkpoint(s) for each `thread_id`; plus: the Session Metadata Store rows, the User Metadata Table item, the Active-run counters item, every Secrets Manager secret under `cei/user-keys/{user_id}/*`, and the user's AgentCore Memory long-term namespace (both strategies). Phase: F19.

---

## Open items carried from the decision log

- Trace chunk key schema (B9) — chunk size is decided (350 KB, B78); the key schema is still proposed, not decided.
- Async job store shape after the B25 Fargate-to-AgentCore-Runtime change — proposed above, not decided in the log itself (the log fixes the requirement, not the schema).
- AgentCore Memory short-term event expiry: exact numeric maximum still needs verification (expected ~365 days); the behavior (B82: "expired, start again") is decided.

## TBD list (this file)

- TBD: Session Metadata Store GSI for cross-user listing (probably unnecessary; omitted for now).
- TBD: Chat History Store GSI for direct user-wide trace queries (avoided by cascading through Session Metadata Store instead).
- TBD: exact SK scheme interleaving turns, trace events, and SSE-replay events (B68) in Chat History Store.
- TBD: Active-run counters table name, and leaked-counter recovery path.
- Export bucket name is deployment-generated; B101 fixes the indexed metadata and Intelligent-Tiering behavior.
- TBD: User Metadata Table email duplication vs. live Cognito read.
- TBD: synchronous vs. fanned-out Deletion Worker execution for large accounts (B73).

Resolved by B67–B109: token-refresh active-run check (B67), SSE event replay via sequence numbers instead of an in-memory buffer (B68), trace chunk size 350 KB (B78), Secrets Manager secret naming `cei/user-keys/{user_id}/{source_id}` (B78), export download URL TTL 15 min (B78), no per-user model restriction attribute (B76), no per-chat delete (B104), account delete 202 + disable/gate + a worker cascade 9 hours later (B73, B109), AgentCore Memory short-term event expiry behavior on expiry ("expired, start again", exact number still to verify — B82).
