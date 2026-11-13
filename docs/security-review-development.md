# Development security review — 2026-11-10

This is a focused source review of report storage and NCBI Gateway access during
the development implementation. It is not a complete security assessment or
evidence about a deployed stack. The worktree was changing during review; the
implementation must be re-reviewed at the final tested commit. No production
deployment, live attack simulation, or user-data access was performed.

## Findings and follow-up

1. **Report write recovery and deletion.** Commit `730a6f5` changed
   `backend/agents/shared/persistence/exports.py` to reserve owner-scoped
   metadata before attempting the S3 write. This addresses the reviewed
   object-first failure in which an ambiguous metadata write could leave a
   report without an indexed owner. An ambiguous S3 write or final status update
   now leaves a `processing` or `failed` reservation for cleanup. The cleanup
   and reconciliation worker is not connected, and the caller must hold a
   deletion fence throughout the write. Without a working fence, deletion can
   remove the reservation while a writer is still uploading and leave an
   unindexed object. Reports have no automatic expiry. Verify failure recovery,
   concurrent deletion and reconciliation in the deployed workflow; the
   reservation change alone does not prove complete deletion.
2. **Deletion fence and cascade are not connected.**
   `backend/lambdas/delete-worker/handler.py` describes a fence, run quiescence,
   and a retryable deletion sequence through an abstract backend. The APIs,
   worker adapters, fence, queue, and full cascade are not wired. The report
   helper assumes writers have already stopped. Implement and prove the fence
   and quiescence before claiming report or account deletion, including a
   concurrent report creation test and retry after partial failure. DynamoDB
   point-in-time recovery is enabled on the export metadata table; document and
   verify the treatment of retained recovery data separately from active records.
3. **Gateway isolation requires enforcement.**
   `backend/lib/config/deploy-config.ts` defaults the policy engine to
   `LOG_ONLY`. In that mode Cedar decisions are observed but not enforced, so
   scope configuration and local tests do not establish source isolation.
   Before using source-scoped tool agents, configure and verify `ENFORCE` in the
   development environment with permitted and denied calls and filtered
   `tools/list` for two different scoped principals. Do not infer live policy
   behavior from a successful synth.
4. **Audit direct Lambda invocation.** The NCBI handler dispatches by the
   Gateway tool name supplied in Lambda client context. The CDK grants the
   Gateway role permission to invoke the function, but does not by itself prove
   that no other principal has an identity-based invocation grant. A principal
   with such a grant can supply client context directly, outside Cedar. No
   untrusted principal with this permission has been identified in this review.
   Audit deployed IAM and resource policies and run a negative direct-invocation
   test before relying on Gateway-only enforcement, especially before adding
   trusted user identity or user API keys. The B99 identity proof and separate
   architecture approval remain prerequisites for user-key integration.

## Controls observed in source

- Report buckets block public access, require TLS, and use S3-managed encryption.
  Metadata is partitioned by authenticated user; listing queries a chat prefix
  without listing S3. The download route reads metadata by owner, checks session
  ownership, and creates a 15-minute presigned URL only on request.
- The export handler's execution role has report object read permission and
  metadata read/query permission. An object writer and deletion worker are not
  yet connected in the reviewed stack.
- The read-only NCBI target and PubMed use the same DynamoDB E-utilities rate
  table; the NCBI target validates operation names and argument shapes. BLAST
  remains unavailable until its request ownership can be protected.

These are source-level observations, not proof of effective deployed policies or
end-to-end behavior. The repository's focused tests exercise owner partitioning,
pagination, targeted schema and IAM configuration, and failure handling with
mocks; they do not substitute for AWS tests.

## Required verification

- Record the tested commit, deployed stack and runtime versions, Gateway policy
  mode, resource policies, principal grants and test environment.
- Exercise two-user report listing/download isolation, forged IDs and cursors,
  no link issuance during listing, link expiry, no S3 bucket scans, encrypted
  storage, and Intelligent-Tiering configuration.
- Verify object and metadata cleanup across account deletion (there is no chat
  delete, B104), including ambiguous write outcomes, retries, concurrent
  writers, backup/recovery policy, and already-issued download links after
  object deletion.
- Validate Gateway scope filtering, direct-invocation denial for relevant
  untrusted principals, rate-limiter behavior under concurrent callers, and
  NCBI errors without leaking input or credentials.
- Complete the broader review of authentication, streaming, Guardrails,
  telemetry, deletion across all delivered stores, and deployed user journeys.
