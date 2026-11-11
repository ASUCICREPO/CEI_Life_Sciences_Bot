# F19 — Data deletion
Status: not started (deferred by the user; `backend/lambdas/delete-worker/` is a tested foundation only, not deployed or wired) | Depends on: F10, F11 | Decisions: B31, B32, B33, B58, B73, B104, B109 | Specs: ../specs/data-model.md, ../specs/api.md

## Goal
Let a user delete their whole account, and have every trace of that data actually removed — history, trace, attachments, KB chunks, checkpoints, long-term memory, and API keys — with the delete request returning immediately, no new run able to start after it, and a worker doing the real cleanup once any in-flight run has had time to end.

There is no chat delete (B104). A user can not delete one chat, and there is no hide/archive action for one chat.

## Scope — In / Out

**In**
- `POST /account/delete` — behind a flag, off by default (B109). Disables the Cognito user (`AdminDisableUser`) and revokes every session (`AdminUserGlobalSignOut`), then writes an account "deleting" gate. The Session Runner and every other run-starting route checks this gate first; no new run can start once it is set. Returns `202 Accepted` at once; the UI hides the account's data immediately.
- Account-delete cascade, run by a scheduled worker 9 hours after the request (8-hour AgentCore run maximum plus 1-hour access-token life — B109): Chat History Store items (turns + trace, including chunked trace items) for every session the user has, Attachment Metadata Store rows plus their S3 objects, KB chunks filtered by `session_id`, the `AgentCoreMemorySaver` checkpoint(s) for each `thread_id`, Session Metadata Store rows, the User Metadata Table item, the Active-run counters item, every Secrets Manager secret under `cei/user-keys/{user_id}/*`, and the user's AgentCore Memory long-term namespace (both `user_preferences` and `semantic_facts`).
- The 9-hour delay itself is the late-write fix (B109): by the time the worker runs, any timed-out SDK retry has already finished, so S3 and Memory extraction need no conditional write.

**Out**
- `DELETE /sessions/{sessionId}` — removed from scope (B104). Do not build a chat-delete route or cascade.
- Deleting long-term memory, attachments, or history for one chat while leaving the rest of the account untouched — there is no chat-scoped delete at all.
- Any UI for reviewing what will be deleted before confirming (not specified by any B-decision; out of scope unless a later phase adds it).
- Deleting in-flight background jobs gracefully mid-step (best-effort cancel is in scope; guaranteeing a clean mid-step stop is not — see Risks).

## Deliverables
- `backend/agents/shared/persistence/chat_history.py` — exists (`append`, `load_page` implemented; `delete_session` raises `NotImplementedError`). Implement `delete_session` (query all items for `session_id`, batch delete, including chunked trace items); the worker calls it once per session the account has.
- `backend/agents/shared/persistence/chat_metadata.py` — exists (`create`, `list_by_user` implemented; `get`, `update_title`, `increment_message_count`, `archive` raise `NotImplementedError`). Add a `delete` method (not currently in the abstract base — add it to `BaseChatMetadataRepository` too) and implement `get` (needed by the delete-worker to look up a session before removing it).
- `backend/agents/shared/persistence/attachment_metadata.py` (new — does not exist yet; F10 was expected to add it) — if F10 has not created it by the time this phase starts, add the minimal repository needed for deletion (`list_by_session`, `list_by_user` via `GSI-1`, `delete`).
- `backend/agents/shared/memory/agentcore.py` — add `delete_checkpoint(thread_id)` and `delete_long_term_namespace(user_id)` helpers.
- `backend/lambdas/account/handler.py` (new) — `POST /account/delete`, behind a flag, off by default (B109). Calls Cognito `AdminDisableUser` and `AdminUserGlobalSignOut`, writes the account "deleting" gate, schedules the delayed sweep (for example an EventBridge Scheduler one-time rule, 9 hours out), and returns `202`.
- A run-start gate check (new, small helper reused by the Session Runner, UiActions, and the Jobs routes) — reads the account "deleting" gate before starting any new run; returns `403 forbidden` (or an equivalent redaction-safe error) if the gate is set.
- `backend/lambdas/delete-worker/handler.py` (new) — triggered by the scheduled sweep, 9 hours after the request; performs the account-delete cascade only. There is no chat-delete path to build.
- `backend/lib/backend-stack.ts` — add the account-delete Lambda, the run-start gate table/attribute, the scheduled-sweep trigger, the delete-worker Lambda, and IAM permissions to disable/sign-out the Cognito user and to delete from DynamoDB tables, S3 buckets (attachments, KB source), the KB (chunk deletion by metadata filter), Secrets Manager (`cei/user-keys/{user_id}/*`), and AgentCore Memory (checkpoint + long-term namespace).

## Tasks
- [ ] 1. Implement `ChatHistoryRepository.delete_session` (batch delete by `session_id`, including chunked trace items from F6's schema).
- [ ] 2. Implement `ChatMetadataRepository.get` and add/implement `delete`.
- [ ] 3. Add the minimal `AttachmentMetadataStore` repository if F10 has not delivered one yet, scoped only to what deletion needs.
- [ ] 4. Add `delete_checkpoint`/`delete_long_term_namespace` to `shared/memory/agentcore.py`.
- [ ] 5. Add `POST /account/delete`: Cognito `AdminDisableUser` + `AdminUserGlobalSignOut`, write the "deleting" gate, schedule the sweep 9 hours out, return `202` (behind a flag, off by default).
- [ ] 6. Add the run-start gate check to every run-starting route (Session Runner, UiActions resume, Jobs), so no new run can start once the gate is set.
- [ ] 7. Build the delete-worker Lambda for the account-delete cascade (per B58's list, now account-only — see Scope above), triggered by the scheduled sweep.
- [ ] 8. Add every IAM permission the disable step and the cascade need.
- [ ] 9. Confirm the frontend hides the account's data immediately after the `202` response (client-side optimistic sign-out), per B73.
- [ ] 10. Do not build `DELETE /sessions/{sessionId}` or any chat-delete cascade (B104).

## Acceptance criteria
- `POST /account/delete` returns `202` immediately; the Cognito user is disabled and signed out of every session before the response returns.
- After the `202` response, no route that starts a new run succeeds for that user — each returns a gate-blocked error instead.
- A running AgentCore session for that user ends on its own at the 8-hour maximum; nothing force-kills it.
- Exactly 9 hours after the request, the delete-worker cascade removes: every session's Chat History Store items, attachments (S3 + metadata), KB chunks, and checkpoint; the Session Metadata rows; the User Metadata item; the Active-run counters item; every `cei/user-keys/{user_id}/*` secret; and both long-term memory strategies' namespace.
- There is no `DELETE /sessions/{sessionId}` route and no way to delete one chat while leaving the rest of the account in place.
- The account-delete route never blocks on the full cascade before responding.

## Tests
- Unit (pytest): the run-start gate check — blocks a new run once the gate is set, allows one before it is set.
- Unit: `delete_session` against a mocked table with turn items, trace items, and chunked trace items — asserts all are gone.
- Unit: delete-worker account-delete cascade, with mocked clients for Cognito, DynamoDB, S3, KB, and AgentCore Memory — asserts it disables/signs out the user up front, removes every session's data, Secrets Manager secrets, and the long-term namespace, and does not skip any session.
- Unit: the scheduled sweep fires at 9 hours, not sooner, from a mocked clock.
- CDK assertion + cdk-nag: the account-delete Lambda, the scheduled-sweep trigger, the delete-worker Lambda IAM policy (scoped, not wildcard, where practical — flag any necessary wildcard with an ADR-format suppression), and that no `DELETE /sessions/{sessionId}` route exists.
- Integration: create a session with a turn, a trace event, an attachment, and a checkpoint; call account delete; assert the user is disabled and a new run is blocked immediately; advance the mocked sweep trigger; assert every store is empty for every session and no account data remains.

## Risks and open items
- **Proposal (not decided), carried from `data-model.md`:** whether the account-delete cascade runs synchronously inside the delete-worker Lambda or is fanned out (SQS, step function) for large accounts — the specific mechanism is this phase's implementation choice, not a decision-log fact.
- Cancelling an in-flight background job (F16) as part of a delete is best-effort: B109 relies on the 8-hour AgentCore run maximum to end any run on its own, rather than force-stopping it. This phase should not claim a guaranteed earlier stop — note it in the worker's own comments and in `docs/architectureDeepDive.md` if this ADR-level tradeoff needs recording.
- KB chunk deletion by metadata filter (S3 Vectors + Bedrock KB) depends on the exact deletion API S5/F10 settle on — this phase assumes a filter-based delete call exists; if F10 only supports a full-reindex-style deletion, this phase's per-session/per-user chunk deletion may need to change.
- The scheduled-sweep mechanism (EventBridge Scheduler one-time rule vs. a delay queue vs. a cron sweep over "deleting" accounts past their 9-hour mark) is this phase's implementation choice; B109 fixes the 9-hour delay, not the trigger mechanism.

## Needs live AWS verification
- `AdminDisableUser` and `AdminUserGlobalSignOut` run against a real Cognito user pool and the user truly can not get a new access token afterward.
- The run-start gate blocks a new run (Session Runner, UiActions resume, and the Jobs routes) within seconds of the `202` response.
- The scheduled sweep fires at 9 hours (not sooner, not much later) and the delete-worker then removes every store in the cascade for a real account with multiple sessions and an attachment.

## Handoff notes

- F5/F6 replay uses separate `sse#<user_sub>#<session_id>#<run_id>` partitions in the Chat History Store. Query the Active-run table with `user_id` and the sort-key prefix `replay#<session_id>#` to enumerate the durable run index, delete each referenced history partition (including its run header and all events), and delete the index records. Deleting only the ordinary session partition is insufficient.
- F20 (Alarms) may want a DynamoDB-throttling alarm to also cover the delete-worker's batch-delete calls (already covered generically by B75's "DynamoDB throttled requests" alarm — no new alarm needed).
- F21 (Hardening) should specifically test the deletion cascade under load (many sessions, many attachments) as part of its load-test pass.
