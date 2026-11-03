# F20 — Alarms
Status: not started | Depends on: F5 | Decisions: B47, B49, B50, B75 | Specs: ../specs/architecture.md, ../specs/security.md

## Goal
Add CloudWatch alarms and one SNS email topic so an operator is notified of API/Lambda errors, security-relevant events, and guardrail intervention spikes — with no agent-health alarm, since F16's watchdog and the dashboard already cover that.

## Scope — In / Out

**In**
- One SNS topic with an admin email address, set as a `deploy.sh` parameter (B47, B50).
- Exactly the three alarm categories B75 fixes:
  1. API and Lambda errors: API Gateway 5xx rate, Lambda errors/throttles, DynamoDB throttling.
  2. Security events: WAF block spikes, Cedar Policy deny spikes, Cognito sign-up spikes.
  3. Guardrail intervention spikes.
- CloudWatch dashboards (B49) — AWS-console-only, not connected to the UI.

**Out**
- Any "stale job" / agent-health alarm — B75 explicitly excludes this ("No agent-health alarms (stale jobs are handled by the watchdog and shown in the dashboard)").
- AgentCore Observability/OTel trace wiring itself — that is a separate B49 item delivered wherever the AgentCore Runtimes are first stood up (F5 and later); this phase only adds the alarms and dashboards on top of whatever metrics already exist by the time it runs.
- The broader candidate alarm list in `security.md` (cost-abuse, per-route throttling alarms, etc.) beyond what B75 fixes — `security.md`'s candidate list predates B75 and is wider than what B75 actually decided; this phase follows B75, not the wider candidate list, per the decision log's own precedence rule.

## Deliverables
- `backend/lib/backend-stack.ts` — add:
  - an SNS Topic + email subscription, parameterized (no hardcoded email),
  - a CloudWatch Alarm for API Gateway 5xx rate,
  - CloudWatch Alarms for Lambda errors/throttles (per function, or a composite — see Tasks),
  - a CloudWatch Alarm for DynamoDB throttled requests (per table with meaningful traffic),
  - a CloudWatch Alarm for WAF blocked-request rate,
  - a CloudWatch Alarm for Cedar Policy deny rate (from AgentCore Policy engine metrics, once F4/S2 exist),
  - a CloudWatch Alarm for Cognito sign-up rate spikes,
  - a CloudWatch Alarm for Bedrock Guardrail intervention rate,
  - a CloudWatch Dashboard aggregating the above.
- `deploy.sh` (from F1) — add the admin email prompt if F1 has not already added a generic "admin notification email" parameter.

## Tasks
- [ ] 1. Add the SNS topic + parameterized email subscription.
- [ ] 2. Add the API Gateway 5xx alarm.
- [ ] 3. Add Lambda error/throttle alarms — **implementation choice, not decided by the log:** one composite alarm across all functions vs. one alarm per function; start with one per function that has meaningful traffic (Session Runner, Session Router, Polling, Settings, delete-worker, watchdog) to keep the alert specific, and revisit if this becomes too noisy.
- [ ] 4. Add DynamoDB throttling alarms for each table with meaningful write/read volume (Chat History, Chat Metadata, Job ID Store, User Metadata, Attachment Metadata).
- [ ] 5. Add the WAF blocked-request-rate alarm.
- [ ] 6. Add the Cedar Policy deny-rate alarm (requires F4/S2's Policy engine to emit a usable metric — coordinate, do not block this phase entirely if the metric is not yet exposed; add a TODO comment matching the existing `backend-stack.ts` style if so).
- [ ] 7. Add the Cognito sign-up-rate alarm.
- [ ] 8. Add the Guardrail intervention-rate alarm (requires F8's Guardrails to exist and emit a metric — same TODO-comment fallback if not yet available).
- [ ] 9. Add the CloudWatch Dashboard.
- [ ] 10. Confirm `deploy.sh` collects and passes the admin email as a stack parameter/context value.

## Acceptance criteria
- Every alarm in B75's three categories exists and points at the one SNS topic.
- No alarm exists for stale/failed background jobs (verified by an explicit "absence" check in the CDK assertion test, since it is easy to accidentally add one by habit).
- The admin email is never hardcoded in the stack; it comes from a CDK parameter/context value set by `deploy.sh`.
- Each alarm's threshold is documented in the stack code as a comment or constant, with a reasonable starting default clearly marked as a starting point, not a validated production value (since B50's own note says "Values TBD" for at least the concurrency-adjacent limits, and B75 does not give numeric thresholds either).

## Tests
- CDK assertion tests: one per alarm confirming it exists, targets the right metric/namespace, and is subscribed to the SNS topic; a negative test confirming no "stale job"/agent-health alarm exists.
- cdk-nag: passes, or every suppression carries an ADR-format reason string.
- Integration (manual, since this is infrastructure-only): trigger a test 5xx burst against a non-production deployment and confirm the SNS email fires.

## Risks and open items
- **B50's numeric threshold values are not decided** (`decision-log.md` "Still open" does not list this explicitly, but `security.md`'s own TBD list flags "final CloudWatch alarm list" as open even after B75 fixed the *categories* — the specific numbers per alarm are this phase's own proposal, not a decision-log fact). Document each chosen threshold clearly as a starting point.
- Guardrail PII filter mode/strictness (B51, open item) does not block this phase, but the Guardrail intervention-rate alarm's meaning depends on it — a stricter filter will naturally trigger more interventions; note this coupling so a future threshold tuning pass understands why the rate might shift when B51's open item is resolved.
- Two alarm sources (Cedar Policy denies, Guardrail interventions) depend on metrics from features that may not exist yet when this phase starts (F4/S2's Policy engine, F8's Guardrails) — this phase should not be blocked entirely on those; ship the alarms that can be built now and add a clear TODO for the rest.

## Handoff notes
- F21 (Hardening) should confirm, as part of its final security review, that every alarm in this phase actually fires under a realistic test condition, not just that the CDK resource exists.
