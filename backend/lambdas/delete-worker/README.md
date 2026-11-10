# Deletion cascade foundation

This is a testable orchestration contract, **not a deployed Lambda**. The file has
no Lambda entry point or production adapters and must not be connected to an SQS
event source yet. Attachment and RAG cleanup are deferred by the user.

`shared.persistence.writer_fence.WriterFence` defines a proposed concrete gate:
a DynamoDB table keyed by `(user_id, scope)` stores account/chat deletion gates
and durable in-flight writer tokens. A writer atomically checks both gates and
session existence while registering its token; report generation holds the token
through the final storage write. A deletion gate must be installed before a
strongly consistent query establishes quiescence. A token is never cleared just
because time passed: recovery must establish that the runtime and all uncertain
remote writes have stopped before releasing it. This foundation has no deployed
table, job/outbox transaction, worker recovery adapter, or universal writer
integration yet; it cannot by itself authorize either deletion route.

Account deletion fences immediately and waits for all chat deletion cascades:
starting a chat deletion increments an account counter in the same transaction
as its chat gate, and completing that cascade decrements it while preserving the
chat tombstone. The account is not quiescent while that counter is nonzero.
The production adapter must durably couple that final transition with job
completion. Token registration can itself time out after committing; recovery
must identify and prove the state of the originating run even when the caller
never received its token. Writer rows include a caller-supplied run reference,
but the deployed job/runtime binding and verified recovery adapter are not yet
implemented. Tests exercise ordered interleavings with Moto; they do not prove
real DynamoDB concurrency or remote-write termination.

Two concrete activation blockers remain:

- A successful SDK operation may follow an earlier timed-out retry whose
  server-side operation is still running. Releasing a writer token after the
  successful response can therefore let deletion finish before that late
  attempt recreates an object. Disabling retries alone does not prove a timed-out
  operation stopped. The implementation needs a demonstrated way to establish
  that every attempt is terminal before releasing the token or declaring
  deletion complete.
- `writer_ref` is supplied by the caller and is not bound to a unique immutable
  runtime execution or to every operation that execution issued. Recovery
  cannot currently use it to establish that the writer and all its remote
  operations have stopped, including after an ambiguous registration response.
  A trustworthy binding and recovery procedure must be implemented and tested.

Keep report generation and deletion routes disabled until both issues, the
durable job/outbox transaction, universal writer coverage, and the remaining
integration and live verification above are complete.

Before activating either deletion route:

1. Authenticate with the Cognito authorizer and derive the user solely from its
   verified claims. Verify session ownership. Atomically write an account/chat
   deletion fence and an owner-bound deletion job to durable storage. Use an
   outbox/stream or equivalent retryable dispatcher so a crash between marking
   and enqueueing cannot strand a job. The queue carries only `job_id`.
2. Make every session, turn, replay, export, memory and job writer atomically
   reject a fenced chat/account. Cancel active jobs, stop AgentCore sessions, and
   confirm quiescence; initiating cancellation is insufficient. Account deletion
   must fence the account before listing sessions so no new sessions can appear.
3. Implement the `DeletionBackend` operations using the actual storage schemas.
   Each must handle pagination, retries and already-deleted data and raise on
   any unprocessed item, missing adapter or uncertain result. Remove S3 export
   objects before their metadata. Include ordinary history and all replay-indexed
   partitions, AgentCore short-term checkpoints, and session metadata last.
4. Account deletion additionally removes both long-term memory namespaces, user
   API keys, user metadata, active-run records, and the Cognito user. Ensure
   asynchronous Secrets Manager deletion meets the intended deletion semantics.
   Never delete user-scoped memory or secrets for a chat-only deletion.
5. `complete_job` must use a conditional, owner-bound state transition and retain
   an idempotency tombstone; completion must never be reported when an adapter is
   unavailable or an underlying operation fails. Configure SQS partial batch
   responses, redrive/DLQ, alarms, scoped IAM and reconciliation for stalled jobs.
6. Only then expose `DELETE /sessions/{sessionId}` and `POST /account/delete`,
   returning 202 after the fence and durable job are committed. Add deployed
   tests for pagination, crashes at each step, SQS redelivery, duplicate requests,
   concurrent writes, cancellation, cross-user access, and missing dependencies.

`process` assumes the backend supplies a trusted job and a deletion fence covering
its owner and scope. The current implementation does not itself provide either.
