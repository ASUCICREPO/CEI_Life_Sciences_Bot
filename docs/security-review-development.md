# Development security review — 2026-11-10

This is a focused source review of report storage and NCBI Gateway access during
the development implementation. It is not a complete security assessment or
evidence about a deployed stack. The worktree was changing during review; the
implementation must be re-reviewed at the final tested commit. No production
deployment, live attack simulation, or user-data access was performed.

## Findings and follow-up

1. **Report write recovery and deletion.** The current
   `backend/agents/shared/persistence/exports.py` writes an S3 object before its
   DynamoDB metadata. If the metadata write fails and the subsequent read also
   fails, the code leaves the object in place. An ambiguous S3 write failure
   can similarly leave an object without metadata. Object keys do not identify
   the owner or session, and `delete_session` enumerates only metadata. Such an
   orphan cannot be found by the current user-scoped deletion helper, and reports
   have no automatic expiry. A recoverable write protocol with durable ownership
   and retryable cleanup is required. A fix is in progress; test failures before
   and after each write, including ambiguous timeouts and concurrent deletion.
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
- Verify object and metadata cleanup across chat and account deletion, including
  ambiguous write outcomes, retries, concurrent writers, backup/recovery policy,
  and already-issued download links after object deletion.
- Validate Gateway scope filtering, direct-invocation denial for relevant
  untrusted principals, rate-limiter behavior under concurrent callers, and
  NCBI errors without leaking input or credentials.
- Complete the broader review of authentication, streaming, Guardrails,
  telemetry, deletion across all delivered stores, and deployed user journeys.
