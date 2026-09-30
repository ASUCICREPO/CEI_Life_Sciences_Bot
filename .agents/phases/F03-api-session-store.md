# F03 — API + session store

Status: not started | Depends on: F2 | Decisions: B2, B12, B22, B59, B78 | Specs: [`../specs/api.md`](../specs/api.md) (Sessions, Chat history routes; error envelope; pagination), [`../specs/data-model.md`](../specs/data-model.md) §1–2 (Session Metadata Store, Chat History Store), [`../specs/auth-and-tool-scope.md`](../specs/auth-and-tool-scope.md) §9/§11

## Goal

Stand up the one Regional REST API with the Cognito authorizer and account-wide throttling, and make session create/list/get/update and turn history readable and writable against real DynamoDB tables. After this phase, a logged-in user can create a chat, see it in a session list, and load its (empty, until F5) history — no agent runs yet.

## Scope — In / Out

**In:** the REST API Gateway (v1, Regional) with the Cognito authorizer on every route, the `/sessions*` and `/sessions/{id}/history` routes (BUFFERED), the Session Metadata Store and Chat History Store tables, the Session Metadata Handler and Session History Handler Lambdas, cursor pagination (B59/B78 defaults), the standard error envelope, and the 20 rps account-wide throttle (B22/B78 — no per-route throttle).

**Out:** `/chat/stream` (F5 adds the STREAM route to this same API), the Jobs/Attachments/Settings/Exports/Account routes (their own later phases), the Active-run concurrency counters enforcement (F5/F16 — this phase can create the counters' schema per `data-model.md` §6 if convenient, but does not need to enforce them since no runs exist yet).

## Deliverables

- `backend/lib/backend-stack.ts` (modify) — add the `RestApi` (Regional endpoint type), the Cognito `CognitoUserPoolsAuthorizer` attached to every method, and the account-wide `UsagePlan`/throttle (20 rps, B78).
- `backend/lambdas/session-metadata-handler/` (new, Python) — `POST /sessions`, `GET /sessions`, `GET /sessions/{sessionId}`, `PATCH /sessions/{sessionId}` (no `DELETE` yet — that's F19).
- `backend/lambdas/session-history-handler/` (new, Python) — `GET /sessions/{sessionId}/history` (turns only; the `/trace` route is F6 scope, once trace events exist).
- `backend/agents/shared/persistence/chat_metadata.py` (modify) — implement `get`, `update_title`, `increment_message_count`, `archive` (currently `NotImplementedError`); this module is reused directly by the Lambda handler, not reimplemented.
- `backend/agents/shared/persistence/chat_history.py` (modify) — confirm `append`/`load_page` support the cursor shape `api.md` defines (`limit`/`cursor` → `items`/`next_cursor`/`has_more`); `delete_session` stays `NotImplementedError` until F19.
- DynamoDB table constructs for `ChatMetadataStore` and `ChatHistoryStore` in `backend-stack.ts`, matching the PK/SK shapes in `data-model.md` §1–2 (no GSIs yet — both are marked Proposal/not-needed-now in the spec).
- Shared error-envelope helper (new, e.g. `backend/lambdas/shared/http_errors.py`) implementing the `api.md` error shape, reused by every REST Lambda from this phase onward.

## Tasks

- [ ] 1. Add the `RestApi` construct (Regional endpoint), the Cognito authorizer, and the account-wide 20 rps `UsagePlan` throttle (B78: no per-route throttle) to `backend-stack.ts`.
- [ ] 2. Add the `ChatMetadataStore` table (`PK=user_id`, `SK=session_id`) and `ChatHistoryStore` table (`PK=session_id`, `SK=sequence_number`), AWS-managed encryption (B52), no TTL (B8/B58).
- [ ] 3. Implement `get`, `update_title`, `increment_message_count`, `archive` in `shared/persistence/chat_metadata.py`; add the `mode` attribute (science/business, B55) and `model_id` attribute (B18) to `ChatMetadataRecord` if not already present.
- [ ] 4. Write the shared error-envelope helper (`code`, `message`, `request_id`, `retriable`) and the cursor-pagination envelope helper (`items`, `next_cursor`, `has_more`), matching `api.md` exactly, including default `limit=20`/max `limit=100` (B78).
- [ ] 5. Write `session-metadata-handler`: `POST /sessions` (create, request `{title, mode, model_id}`), `GET /sessions` (paginated list), `GET /sessions/{sessionId}` (single get), `PATCH /sessions/{sessionId}` (partial update of `title`/`mode`).
- [ ] 6. Write `session-history-handler`: `GET /sessions/{sessionId}/history?limit=&cursor=` returning turns only, newest-page-first-then-chronological per B59, using `ChatHistoryStore.load_page`.
- [ ] 7. Wire both Lambdas' IAM roles to only the tables they need (least privilege, per `security.md`'s IAM control).
- [ ] 8. Add per-user isolation checks: every handler must derive `user_id` from the Cognito authorizer's claims (never from a request body/path parameter) before any table read/write, per B12.
- [ ] 9. Add CDK assertion tests and cdk-nag suppressions (ADR format) for any new findings on the API/table constructs.

## Acceptance criteria

- A Cognito-authenticated `POST /sessions` creates a session record visible to `GET /sessions` for that same user, and invisible to a different authenticated user.
- `GET /sessions/{sessionId}/history` returns an empty, correctly-shaped paginated envelope for a session with no turns yet.
- `PATCH /sessions/{sessionId}` updates `title`/`mode` and is reflected in a subsequent `GET`.
- A request with no Cognito token, or an expired token, is rejected with `401 unauthorized` in the standard error envelope, on every route in this phase.
- A request exceeding the 20 rps account-wide throttle receives `429` mapped to `rate_limited` in the standard error envelope.
- `npx cdk synth` passes cdk-nag with ADR-format suppressions only.

## Tests

- Jest/pytest (matching each Lambda's language — these are Python per B3) unit tests for `session-metadata-handler` and `session-history-handler`: create/list/get/update happy paths, cross-user isolation (user A cannot `GET` user B's session), pagination cursor round-trip, malformed request → `invalid_request`.
- `pytest` unit tests for the newly implemented `chat_metadata.py` methods (`get`, `update_title`, `increment_message_count`, `archive`), extending the existing test patterns under `backend/agents/tests/`.
- CDK assertion tests: `RestApi` has the Cognito authorizer on every method in this phase; the `UsagePlan` throttle is 20 rps; both tables have no public access and AWS-managed encryption.
- Integration check (manual or scripted): full `POST /sessions` → `GET /sessions` → `PATCH` → `GET /sessions/{id}/history` sequence against a deployed environment with a real Cognito token.

## Risks and open items

- `data-model.md` §1 flags a `GSI-1` for cross-session-user listing as "Proposal (not decided), omit until a phase needs it" — do not add it in this phase; no access pattern in this phase's routes needs it.
- The trace-storage schema addition to `ChatHistoryStore` (`record_type: trace_event`, chunking for entries over 400 KB) is F6 scope, not this phase — `load_page`'s implementation in this phase should tolerate a future `record_type` attribute existing on some items without needing to understand it yet (i.e., don't hardcode an assumption that every item is a turn).
- Default/maximum pagination `limit` (20/100) is stated as B78-resolved in the decision log; if the spec text you read still shows it as a proposal, follow the decision log, not the spec's older wording.

## Handoff notes

- F5 (Research agent live) adds `/chat/stream` to this same `RestApi` construct (not a new API) and writes real turns into `ChatHistoryStore` via `chat_history.append`, using the pagination/error conventions this phase establishes.
- F6 (Streaming UI) extends `ChatHistoryStore`'s schema for trace events and adds the `/sessions/{id}/turns/{messageId}/trace` route — it must not change the turn-only shape this phase's `GET /history` route already returns.
- F19 (Data deletion) implements `chat_metadata.py`'s and `chat_history.py`'s still-`NotImplementedError` delete methods and adds the `DELETE` routes this phase intentionally leaves out.
