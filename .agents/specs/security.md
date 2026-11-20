# Security Spec

Status: planned. Source of truth is `.agents/decision-log.md` (B1–B86). Where this spec conflicts with `SECURITY.md`, `docs/architectureDeepDive.md`, or `docs/agent-architecture-decisions.md`, the decision log wins.

Related specs (not owned here): `.agents/specs/architecture.md`, `.agents/specs/agents.md`, `.agents/specs/tools.md`, `.agents/specs/auth-and-tool-scope.md` (Cedar Policy and Cognito M2M scoped tokens in detail — this spec references but does not redefine that design), `.agents/specs/data-model.md` (encryption, deletion), `.agents/specs/api.md` (throttling, error redaction), `.agents/specs/streaming-protocol.md` (trace redaction in `trace_event`).

Existing security-relevant code this spec extends: `backend/agents/shared/schemas/cei_ui.py` (`ARTIFACT_HTML_DENYLIST`), `frontend/lib/cei-ui/schema.ts` (its TypeScript mirror), `backend/agents/shared/transport/sse.py` (current diagnostic redaction), `docs/cei-ui-protocol.md` "Artifact Security" (D17).

## Threat model

| # | Threat | Vector | Impact | Primary mitigation |
|---|---|---|---|---|
| T1 | Prompt injection | Malicious content in tool results (data source APIs), uploaded documents (RAG chunks), or Web Search snippets steers the model into unintended actions or disclosure | Data exfiltration, unauthorized tool calls, unsafe output | Guardrails (prompt-attack check) on every model call (B51); tool results and retrieved chunks are treated as untrusted data, never as instructions, by the agent prompt design (`.agents/specs/agents.md`); Cedar default-deny limits what an injected instruction could even call (B30) |
| T2 | Cross-user data access | A bug or crafted request reads another user's chat, attachment, or memory | Confidentiality breach, regulatory exposure (life-sciences data) | Every table partitioned by Cognito `sub` (B12, `.agents/specs/data-model.md`); Gateway Lambdas read only the calling user's Secrets Manager key, keyed by `sub` (B30); IAM least privilege per Lambda/runtime |
| T3 | Token scope escalation | A tool agent or specialist agent obtains a token scoped to more data sources than it should have | A single-source tool agent silently gains cross-source access | Cognito resource server with one scope per source; the Session Router holds the only M2M client secret; tokens are minted per request with only the allowed scopes; a pre-token-generation trigger adds `sub` + session ID (B30); Cedar Policy is default-deny and filters `tools/list` so a scope-mismatched agent cannot even see a tool exists |
| T4 | Artifact sandbox escape | Model-authored `Artifact.html` attempts to read parent-origin data, exfiltrate via network/storage, or navigate away from its sandboxed document | Data exfiltration from the page the artifact is embedded in, or a convincing spoofed UI outside the sandbox | `sandbox="allow-scripts"` only (no `allow-same-origin`, opaque origin), CSP `<meta>` tag denying all network/storage, empty `allow` attribute, one-shot resize message channel with strict origin/shape checks, navigation tripwire that unmounts on a second `onLoad`, `ARTIFACT_HTML_DENYLIST` as defense in depth (D17, `docs/cei-ui-protocol.md` "Artifact Security" — this spec does not restate the full control list, only references it) |
| T5 | Cost abuse via open sign-up | A self-sign-up user (B13, no MFA per B14) scripts high-volume model/tool usage | Unbounded Bedrock/tool spend | Email domain allowlist at sign-up (Cognito pre-sign-up trigger, B16); API Gateway 20 rps throttle (B22); per-user concurrency limits (2 real-time + 1 background run, B22); WAF rate rule; no token/cost budget in the MVP is an accepted gap (B16 — see "Accepted gaps" below) |
| T6 | Secrets leakage in trace | Model reasoning, tool inputs/outputs, or error messages surfaced in the trace or SSE stream leak model IDs, guardrail config, prompt text, or API keys | Prompt/IP disclosure, credential leakage | B7's redaction rule, implemented today in `shared/transport/sse.py` and extended in `.agents/specs/streaming-protocol.md`'s `trace_event`; never-show list: model IDs, guardrail/system prompts, prompt versions, secrets |

## Controls by category

### Identity and access

- **Cognito** — self-sign-up with email verification (B13); no MFA (B14, accepted gap); email domain allowlist enforced by a pre-sign-up Lambda trigger (B16).
- **Cedar Policy (AgentCore Policy engine)** — one Gateway with all source targets, one default-deny policy, `tools/list` filtered per caller's scopes (B30). Full design owned by `.agents/specs/auth-and-tool-scope.md`. Rollout: start in `LOG_ONLY`, then flip to `ENFORCE` (B30) — this two-step rollout is itself a control against shipping an over-permissive policy silently.
- **Cognito M2M scoped tokens** — one resource server, one scope per data source; the Session Router is the only holder of the M2M client secret; tool agents get one source scope, specialist agents get all source scopes (B26/B30).
- **IAM least privilege** — each Lambda and each AgentCore runtime gets its own execution role scoped to only the tables/buckets/secrets it needs (for example, only the Attachment Processor role can write `AttachmentMetadataStore`; only Gateway Lambdas can read `cei/user-keys/*` secrets, and only for the calling user's prefix).
- **Profile PII and the age gate (B112, built in code, not yet deployed)** — `given_name`, `family_name`, and `birthdate` are personal data. They are written once, through `POST /profile`, as standard Cognito user attributes; no copy is kept in any application table or log. The Profile Lambda, the pre-token-generation trigger's new age-check branch, and every route that checks the `cei_age_verified` claim must never write the user's name or date of birth to CloudWatch Logs or the Chat History trace — this extends B7's never-show list to this new PII. The age check itself (18+) runs once, server-side, from the stored `birthdate`; the result surfaces only as the boolean-shaped `cei_age_verified` claim, never as the date of birth itself, on any token or in any log line.

### Network / edge

- **WAF** in front of CloudFront: AWS-managed rule groups plus a rate-based rule (T5 mitigation).
- **API Gateway throttling** — only the global 20 rps account-wide throttle (B22, B78); no per-route method throttling (B78 — resolves the earlier "candidate refinement" open item).
- **CORS** — the API allows only the deployed frontend origin(s); no wildcard. Proposal (not decided): whether local development origins are allowed in non-production deployments via `deploy.sh` configuration.
- **CSP on CloudFront** — a top-level `Content-Security-Policy` response header on the frontend distribution. This must be written to deliberately permit what the Artifact sandbox's own CSP `<meta>` tag allows (inline script/style inside the iframe, `data:` images) without weakening the top-level page's own policy — this is the open dependency already flagged in `docs/cei-ui-protocol.md` ("CSP inheritance" residual risk) and is not yet solved; it is deployment work owned by `.agents/specs/architecture.md` / the CDK stack, tracked here as a security dependency.

### Model calls

- **Bedrock Guardrails** on every agent model call, no exceptions (B51). Guardrail configuration itself is never shown in the trace (B7).
- **PII/PHI masking** applies to user input only, not to tool results or model output (B51a) — deliberately, because public NPI/author data legitimately contains names and would otherwise be broken by a strict output filter. The mode is decided: MASK (anonymize) on user input only (B51a). Open: the exact list of PII/PHI entity types.

### Trace and stream redaction

- Implemented today in `shared/transport/sse.py`: thinking, tool name/input, and code are replaced with generic status placeholders; error messages collapse to a fixed safe string; error codes collapse to one of four allowed values.
- Planned (F6, `.agents/specs/streaming-protocol.md`): `trace_event` is richer than today's placeholders — it shows real tool names, inputs, outputs, sources, and reasoning text (labeled `"summarized"` when the provider summarized it) — but keeps the same never-show list (model IDs, guardrail/system prompts, prompt versions, secrets), per B7. This is a deliberate widening of what the trace shows, not a loosening of what it protects; implementers should treat the never-show list as the actual security boundary, not the current placeholder behavior.

### Artifact sandbox

Fully specified in `docs/cei-ui-protocol.md` "Artifact Security" (D17); this spec does not restate it. Summary for threat-model traceability only: `sandbox="allow-scripts"` with no `allow-same-origin` (opaque origin), a restrictive CSP `<meta>` tag, a one-shot origin/shape-checked resize message channel, a navigation tripwire, and `ARTIFACT_HTML_DENYLIST` (Python) / its TypeScript mirror as defense in depth. The sandbox attributes and CSP are the actual boundary; the denylist is not.

### Encryption

AWS-managed keys for DynamoDB, S3, and Secrets Manager; no customer-managed KMS key (B52). cdk-nag suppressions for any encryption-related finding require an ADR-format reason string: `ADR: <title> | Rationale: <why> | Alternative: <rejected>` (existing repo convention, `CLAUDE.md`).

### Observability and alerting

CloudWatch alarms feed one SNS topic with an admin email set by `deploy.sh` (B50). Alarm list is decided (B75 — resolves the earlier B50 open item), in three groups:

1. **API and Lambda errors:** API Gateway 5xx rate, Lambda errors/throttles, DynamoDB throttling.
2. **Security events:** WAF block spikes, Cedar Policy deny rate spike (T3 signal), Cognito sign-up spikes (T5 signal).
3. **Guardrail intervention spikes** (T1 signal — many prompt-attack blocks in a short window).

**No agent-health alarms (B75, explicit):** stale/failed jobs are handled by the F16 watchdog and surfaced in the dashboard job view, not by a CloudWatch alarm.

### Infrastructure as code hygiene

cdk-nag `AwsSolutionsChecks` scoped to the stack (existing repo convention, `CLAUDE.md`); every suppression must carry the ADR-format reason string above. This is a process control against silently disabling a security check without a recorded rationale.

## Accepted gaps (explicitly decided, not oversights)

- No MFA (B14).
- No token/cost budget per user in the MVP — only request-rate and concurrency limits (B16).
- No customer-managed KMS key (B52).
- PII/PHI filtering does not apply to tool results or model output (B51a).

## Open items carried from the decision log

- Guardrail PII/PHI entity type list (mode is decided in B51a: mask, user input only).
- AgentCore Memory short-term event expiry: the exact maximum number needs verification against AWS docs (expected ~365 days); the behavior on expiry is decided (B82 — a waiting HITL pause shows "expired, start again" rather than staying stuck indefinitely, so the earlier "stuck wait state" availability concern is resolved in behavior, pending only the numeric confirmation).

## TBD list (this file)

- TBD: whether non-production `deploy.sh` configurations allow additional CORS origins for local development.
- TBD: production CloudFront CSP header content, and its interaction with the Artifact sandbox's own CSP (cross-ref `docs/cei-ui-protocol.md` "CSP inheritance").

Resolved by B67–B86: CloudWatch alarm list, with explicitly no agent-health alarms (B75); no per-route API Gateway method throttling, only the global 20 rps limit (B78); async threshold and no mid-run handoff (B79); HITL expiry behavior (B82); the Web Search connector is Cedar-gated as a Gateway target while Code Interpreter is not (B86, relevant to T3's scope-escalation boundary).
