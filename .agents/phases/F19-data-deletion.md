# F19 — Data deletion
Status: not started | Depends on: F10, F11 | Decisions: B31, B32, B33, B58, B73 | Specs: ../specs/data-model.md, ../specs/api.md

## Goal
Let a user delete one chat, or their whole account, and have every trace of that data actually removed — history, trace, attachments, KB chunks, checkpoints, and (for account delete) long-term memory and API keys — with the delete request returning immediately and a worker doing the real cleanup.

## Scope — In / Out

**In**
- `DELETE /sessions/{sessionId}` — chat delete cascade (B58): Chat History Store items (turns + trace, including chunked trace items), Attachment Metadata Store rows for that session plus their S3 objects, KB chunks filtered by `session_id`, and the `AgentCoreMemorySaver` checkpoint(s) for that `thread_id`.
- `POST /account/delete` — account delete cascade: everything above for every session the user has, plus Session Metadata Store rows, the User Metadata Table item, the Active-run counters item, every Secrets Manager secret under `cei/user-keys/{user_id}/*`, and the user's AgentCore Memory long-term namespace (both `user_preferences` and `semantic_facts`).
- Both routes return `202 Accepted` immediately and mark the item "deleting"; a worker performs the real removal; the UI hides the item at once (B73).

**Out**
- Deleting long-term memory as part of a *chat* delete — B58 is explicit that chat delete "does not touch long-term memory... those are user-scoped, not chat-scoped."
- Any UI for reviewing what will be deleted before confirming (not specified by any B-decision; out of scope unless a later phase adds it).
- Deleting in-flight background jobs gracefully mid-step (best-effort cancel is in scope; guaranteeing a clean mid-step stop is not — see Risks).

## Deliverables
- `backend/agents/shared/persistence/chat_history.py` — exists (`append`, `load_page` implemented; `delete_session` raises `NotImplementedError`). Implement `delete_session` (query all items for `session_id`, batch delete, including chunked trace items).
- `backend/agents/shared/persistence/chat_metadata.py` — exists (`create`, `list_by_user` implemented; `get`, `update_title`, `increment_message_count`, `archive` raise `NotImplementedError`). Add a `delete` method (not currently in the abstract base — add it to `BaseChatMetadataRepository` too) and implement `get` (needed by the delete-worker to look up a session before removing it).
- `backend/agents/shared/persistence/attachment_metadata.py` (new — does not exist yet; F10 was expected to add it) — if F10 has not created it by the time this phase starts, add the minimal repository needed for deletion (`list_by_session`, `list_by_user` via `GSI-1`, `delete`).
- `backend/agents/shared/memory/agentcore.py` — add `delete_checkpoint(thread_id)` and `delete_long_term_namespace(user_id)` helpers.
- `backend/lambdas/delete-worker/handler.py` (new) — SQS-triggered; performs the actual cascade for a chat-delete or account-delete message.
- `backend/lambdas/sessions/handler.py` or equivalent (new/extended, depending on what F3 built) — `DELETE /sessions/{sessionId}` marks the session `deleting` and enqueues a delete-worker message; returns `202`.
- `backend/lambdas/account/handler.py` (new) — `POST /account/delete` marks the user's sessions `deleting` (or a single account-level flag) and enqueues an account-delete-worker message; returns `202`.
- `backend/lib/backend-stack.ts` — add the SQS queue(s) for delete work, the delete-worker Lambda, IAM permissions to delete from DynamoDB tables, S3 buckets (attachments, KB source), the KB (chunk deletion by metadata filter), Secrets Manager (`cei/user-keys/{user_id}/*`), and AgentCore Memory (checkpoint + long-term namespace).

## Tasks
- [ ] 1. Implement `ChatHistoryRepository.delete_session` (batch delete by `session_id`, including chunked trace items from F6's schema).
- [ ] 2. Implement `ChatMetadataRepository.get` and add/implement `delete`.
- [ ] 3. Add the minimal `AttachmentMetadataStore` repository if F10 has not delivered one yet, scoped only to what deletion needs.
- [ ] 4. Add `delete_checkpoint`/`delete_long_term_namespace` to `shared/memory/agentcore.py`.
- [ ] 5. Build the delete-worker Lambda: chat-delete cascade (per B58's chat-delete list above).
- [ ] 6. Extend the delete-worker (or add a second handler) for the account-delete cascade (per B58's account-delete list above).
- [ ] 7. Add `DELETE /sessions/{sessionId}` — mark `deleting`, enqueue, return `202`.
- [ ] 8. Add `POST /account/delete` — mark, enqueue, return `202`.
- [ ] 9. Add the SQS queue(s), delete-worker Lambda, and every IAM permission the cascade needs.
- [ ] 10. Confirm the frontend hides a "deleting" session immediately in the session list (client-side optimistic removal), per B73.

## Acceptance criteria
- `DELETE /sessions/{sessionId}` returns `202` immediately; within a bounded worker-processing time, the session's Chat History Store items, its attachments (S3 + metadata), its KB chunks, and its checkpoint are all gone.
- A chat delete never removes the user's long-term memory or API keys.
- `POST /account/delete` returns `202` immediately; the account-delete cascade removes everything a chat delete removes for every session, plus the Session Metadata rows, User Metadata item, Active-run counters item, every `cei/user-keys/{user_id}/*` secret, and both long-term memory strategies' namespace.
- No delete route ever blocks on the full cascade before responding.

## Tests
- Unit (pytest): `delete_session` against a mocked table with turn items, trace items, and chunked trace items — asserts all are gone.
- Unit: delete-worker chat-delete cascade, with mocked clients for DynamoDB, S3, KB, and AgentCore Memory — asserts each is called with the correct filter/key.
- Unit: delete-worker account-delete cascade — asserts it also removes Secrets Manager secrets and the long-term namespace, and does not skip any session.
- CDK assertion + cdk-nag: SQS queue(s), delete-worker Lambda IAM policy (scoped, not wildcard, where practical — flag any necessary wildcard with an ADR-format suppression), the two new routes.
- Integration: create a session with a turn, a trace event, an attachment, and a checkpoint; call chat delete; assert every store is empty for that session and long-term memory/API keys are untouched; repeat for account delete and assert everything is gone.

## Risks and open items
- **Proposal (not decided), carried from `data-model.md`:** whether deletion is synchronous inside the delete Lambda or fanned out (SQS, as this phase assumes) for large accounts — this phase picks SQS fan-out as the concrete design since B73 already requires an async `202` response pattern; the specific mechanism (single worker vs. queue) is this phase's implementation choice, not a decision-log fact.
- Cancelling an in-flight background job (F16) as part of a delete is best-effort: the cooperative cancel flag plus `StopRuntimeSession` may not stop a job mid-Lambda-invocation instantly. This phase should not claim a guaranteed clean stop — note it in the worker's own comments and in `docs/architectureDeepDive.md` if this ADR-level tradeoff needs recording.
- KB chunk deletion by metadata filter (S3 Vectors + Bedrock KB) depends on the exact deletion API S5/F10 settle on — this phase assumes a filter-based delete call exists; if F10 only supports a full-reindex-style deletion, this phase's per-session/per-user chunk deletion may need to change.

## Handoff notes

- F5/F6 replay uses separate `sse#<user_sub>#<session_id>#<run_id>` partitions in the Chat History Store. Query the Active-run table with `user_id` and the sort-key prefix `replay#<session_id>#` to enumerate the durable run index, delete each referenced history partition (including its run header and all events), and delete the index records. Deleting only the ordinary session partition is insufficient.
- F20 (Alarms) may want a DynamoDB-throttling alarm to also cover the delete-worker's batch-delete calls (already covered generically by B75's "DynamoDB throttled requests" alarm — no new alarm needed).
- F21 (Hardening) should specifically test the deletion cascade under load (many sessions, many attachments) as part of its load-test pass.
