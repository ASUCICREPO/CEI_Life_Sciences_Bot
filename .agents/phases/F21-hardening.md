# F21 — Hardening
Status: not started | Depends on: all (F1–F20, S1–S5) | Decisions: B22, B52, B58, B65 (and every decision touched by the security review) | Specs: ../specs/security.md, ../specs/architecture.md, all other specs (final consistency pass)

## Goal
Before calling the system launch-ready: run the project's own security review command, load-test the streaming and background paths against the concurrency/rate limits already decided, finish every human-facing doc in `docs/`, and clean up the `archive/` directory left over from the frontend replacement (B66).

## Scope — In / Out

**In**
- Run `/cic-security-check` against the full repository and remediate or explicitly accept (with a written reason) every finding.
- Load test `/chat/stream` and the background-job path (`/jobs/*`) at and slightly above the B22 limits (20 rps per user, 2 concurrent real-time runs, 1 concurrent background run) to confirm the limits actually hold and degrade safely (`rate_limited`/`concurrency_limit` errors, not crashes or cross-user leakage).
- Complete, in `docs/`: `userGuide.md`, `deploymentGuide.md`, `APIDoc.md`, `modificationGuide.md`, `provisioned-resources.md`, `projectClosure.md` — all describing the system as actually built by this point, not the target state (B65's placeholder rule no longer applies once F1–F20 are done).
- Archive cleanup: audit `archive/frontend-next/` (created in F2 per B66) and remove anything that is no longer needed for reference, keeping only what a future maintainer would actually want (for example, the CEI UI renderer and zod schema B66 calls out by name).

**Out**
- New features. This phase fixes, documents, and verifies; it does not add agent capability.
- Anything the security review or load test surfaces that requires a new architectural decision (not just a fix) — escalate that as a new decision-log entry (a new `Bxx`) rather than silently deciding it inside this phase.

## Deliverables
- `docs/userGuide.md` — end-user walkthrough of chat, tools, pipelines, settings, exports, and data deletion, reflecting the real, shipped UI (founder-ai-dashboard-derived `frontend/`).
- `docs/deploymentGuide.md` — the real `deploy.sh` flow, CDK stack contents, Region requirements (per B45's service-availability check), and configuration knobs actually exposed.
- `docs/APIDoc.md` — every route from `.agents/specs/api.md`, updated to match what was actually implemented (note any deviation from the spec discovered during implementation).
- `docs/modificationGuide.md` — how to add a new data source, a new tool-agent profile, a new export format, or a new alarm, pointing at the real file locations from F4–F20.
- `docs/provisioned-resources.md` — an exact list of what is deployed (per B65's rule: only what is really deployed, nothing planned-but-not-built).
- `docs/projectClosure.md` (new) — summary of what shipped, what was deferred (cross-reference `later.md`), and any open `Bxx`/TBD items still outstanding at launch.
- `scripts/load-test/` (new) — load test scripts/config (tool choice is an implementation detail; k6 or Artillery are reasonable defaults) for `/chat/stream` and `/jobs/*`.
- Updated `SECURITY.md` if `/cic-security-check` finds anything that changes the documented security posture.

## Tasks
- [ ] 1. Run `/cic-security-check` against the repository; triage every finding as fix-now, accept-with-reason, or escalate-as-new-decision.
- [ ] 2. Fix what can be fixed in this phase; record any accepted risk in `SECURITY.md`'s "Accepted gaps" section (matching `security.md`'s existing pattern).
- [ ] 3. Write and run the load test for `/chat/stream` at the per-user rate and concurrency limits (B22); confirm the 21st request/second and the 3rd concurrent real-time run get a clean `rate_limited`/`concurrency_limit` response, not a crash.
- [ ] 4. Write and run the load test for `/jobs/*` at the 1-concurrent-background-run limit; confirm the same clean-degradation behavior.
- [ ] 5. Write `docs/userGuide.md` against the real, shipped `frontend/`.
- [ ] 6. Write `docs/deploymentGuide.md` against the real `deploy.sh` and CDK stack.
- [ ] 7. Write `docs/APIDoc.md` against the real, implemented routes (diff against `.agents/specs/api.md` and note any drift).
- [ ] 8. Write `docs/modificationGuide.md`.
- [ ] 9. Write `docs/provisioned-resources.md` listing only what `cdk deploy` actually created.
- [ ] 10. Write `docs/projectClosure.md`.
- [ ] 11. Audit `archive/frontend-next/`; remove anything not worth keeping for reference; keep and clearly label what B66 calls out (CEI UI renderer, zod schema).

## Acceptance criteria
- `/cic-security-check` has been run and every finding has a recorded disposition (fixed, accepted with a written reason, or escalated to a new decision-log entry).
- The load test demonstrates the B22 limits hold under load without cross-user data leakage or an unhandled crash.
- Every doc file listed above exists, is non-empty, and describes only what was actually built (spot-check: no "planned" language remains for anything that shipped).
- `archive/frontend-next/` contains only material with a clear, stated reason to keep it.

## Tests
- The security review itself is the primary "test" for this phase; its output is the artifact.
- Load test pass/fail criteria: error rate and latency under the documented limits, and a clean (not silent) failure mode above them.
- A documentation completeness check: each of the six `docs/` files listed exists and each references only resources confirmed present in `docs/provisioned-resources.md`.
- No automated unit/integration tests are expected to be net-new in this phase beyond the load-test scripts themselves; this phase should not introduce new application code.

## Risks and open items
- Every "Proposal (not decided)" item still open across F1–F20's phase files (alarm numeric thresholds, BLAST poll interval, the tool-agent per-profile hook interface, tool-agent long-term-memory eligibility, deletion synchronous-vs-fanned-out, Router low-confidence threshold value, and others) should be listed explicitly in `docs/projectClosure.md` as known open items at launch, not silently dropped. Note that the async threshold (B79) and the Pipeline Manager dispatch mechanism (B83) are no longer open — they were resolved in the decision log after this file was first written; do not re-list them as open.
- If `/cic-security-check` surfaces a finding that implies a real architectural change (not just a code fix), this phase must not decide it unilaterally — record it as a new `Bxx` candidate for the decision-log owner and escalate, per this agent's own scope-discipline rule.
- Load testing against real AWS resources has a real dollar cost (Bedrock calls, AgentCore sessions) — scope the load test's duration/volume deliberately to bound spend, since B16 already accepts "no token/cost budget" as an MVP gap; this phase's own test runs should not be the thing that triggers that gap in practice.

## Handoff notes
- This is the last phase in the list. Its `docs/projectClosure.md` is the single artifact anyone picking up the project after launch should read first, alongside `.agents/decision-log.md`.
