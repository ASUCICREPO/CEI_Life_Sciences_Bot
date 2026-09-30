# F01 — Deploy foundation

Status: not started | Depends on: — | Decisions: B45, B46, B47, B48, B52 | Specs: [`../specs/architecture.md`](../specs/architecture.md) (Deploy foundation row, §1 component table), [`../specs/security.md`](../specs/security.md) (Infrastructure as code hygiene, Encryption)

## Goal

Give every later phase somewhere real to deploy to: a guided `deploy.sh` that collects configuration and starts a CodeBuild deployment, and a CDK skeleton with the conventions (Region configurability, cdk-nag, ADR-format suppressions) that every subsequent phase's CDK code must follow. No product feature ships in this phase.

## Scope — In / Out

**In:** `deploy.sh` script, a CodeBuild project definition, the CDK app/stack skeleton extension (beyond today's Code Interpreter-only stack), Region configurability via CDK context/env vars, a Region-capability check (AgentCore Runtime/Gateway/Memory/Policy, S3 Vectors, Bedrock KB, selected models available in the chosen Region), and the single-environment convention (B46).

**Out:** any actual product resource (Cognito, API Gateway, Gateway targets, tables) — those belong to F2+. This phase only builds the deployment mechanism and the skeleton those resources will be added to.

## Deliverables

- `deploy.sh` (new, repo root or `backend/`) — interactive/guided script collecting: target AWS account/Region, model allowlist/blocklist values, usage limits, allowed sign-up email domains, admin alert email, then invoking CDK synth/deploy through CodeBuild.
- `backend/lib/backend-stack.ts` (modify) — add a Region-capability precheck construct or a `deploy.sh`-invoked pre-flight script; keep the existing `CodeInterpreterCustom` resource and its cdk-nag suppression conventions unchanged.
- `backend/bin/backend.ts` (modify) — read Region from CDK context/env instead of leaving `env` fully commented out; keep the stack environment-agnostic only where `deploy.sh` has not yet supplied a Region.
- `backend/lib/config/` (new) — a typed config module CDK reads from `deploy.sh`'s generated context file (model blocklist, limits, email domains, admin email, Region).
- `buildspec.yml` (new, repo root or `backend/`) — CodeBuild project spec: install deps, `npm run build`, `npx cdk deploy`.
- CDK unit test additions under `backend/test/` for the new config-loading and Region-check logic.

## Tasks

- [ ] 1. Write `backend/lib/config/deploy-config.ts` (or similar): a typed loader for the values `deploy.sh` collects (Region, model blocklist, limits, email allowlist, admin email), reading from a CDK context JSON file `deploy.sh` writes.
- [ ] 2. Write `deploy.sh`: prompt for (or accept via flags) target Region, model blocklist entries, per-user limits (B22 defaults — see F3/S2 for where these are enforced), allowed email domains (B16), and admin alert email (B50); write them to the CDK context file from task 1.
- [ ] 3. Add a Region-capability precheck to `deploy.sh` (or a small companion script it calls) that verifies the selected Region has AgentCore Runtime, Gateway, Memory, and Policy; S3 Vectors; Bedrock Knowledge Base; and the configured default/allowed Bedrock models — fail with a clear message before CDK deploy starts if any are missing (per B45's requirement).
- [ ] 4. Add region-gating for the AgentCore Web Search connector (B44: only us-east-1, eu-west-1, ap-northeast-1) to the same precheck — outside those Regions, `deploy.sh` must record "web search disabled" in the config rather than failing (web search itself is F12 scope; this phase only wires the config flag).
- [ ] 5. Write `buildspec.yml` and the CodeBuild project construct in `backend-stack.ts` (or a separate `deploy-pipeline-stack.ts`, if keeping the pipeline construct out of the product stack is cleaner — note the choice made in this phase file's Handoff notes so F2+ know which stack to add resources to).
- [ ] 6. Update `backend/bin/backend.ts` to pass the Region from the config loader into the stack's `env`, replacing today's fully-commented-out block.
- [ ] 7. Confirm existing cdk-nag `AwsSolutionsChecks` scoping (stack-level, not app-level — the existing ADR in `backend-stack.ts`) still applies once the CodeBuild/pipeline construct is added; add any new suppressions needed, each with the ADR-format reason string (`CLAUDE.md` convention).
- [ ] 8. Run `npx cdk synth` and `npm test` in `backend/` and confirm the skeleton still synthesizes cleanly with the existing Code Interpreter resource plus the new config/pipeline additions.

## Acceptance criteria

- `deploy.sh` run end to end (against a real or sandbox AWS account) produces a CDK context file, triggers a CodeBuild deployment, and the deployment succeeds with no product resources yet — just the config-aware skeleton.
- Running `deploy.sh` against a Region missing a required AgentCore capability fails fast with a clear, specific error naming the missing capability, before any CDK deploy is attempted.
- Running `deploy.sh` against a Region outside the three Web Search-capable Regions completes successfully but records the web-search-disabled flag in the generated config.
- `npx cdk synth` succeeds; cdk-nag reports no unsuppressed findings, and every suppression carries an ADR-format reason string.
- `npm test` (Jest) passes for the new config-loading unit tests.

## Tests

- Jest unit tests under `backend/test/` for: the config loader (valid/invalid context JSON), the Region-capability precheck logic (mocked service-availability responses), and the Web Search Region-gating logic.
- CDK assertion tests (`aws-cdk-lib/assertions`) confirming the CodeBuild project and any new IAM roles are created with expected properties.
- cdk-nag: `npx cdk synth` must produce zero unsuppressed `AwsSolutionsChecks` findings.
- No integration/e2e test in this phase (no product feature to exercise yet); a real `deploy.sh` dry run against a sandbox account is the closest thing to an integration check and should be done manually before marking this phase done.

## Risks and open items

- B46 ("single environment, developers use local AgentCore CLI runs") means this phase does not need to build multi-environment plumbing — keep `deploy.sh` and the CDK config single-environment-only; do not add an environment-name parameter unless the user asks for one later.
- The exact list of Region-required AgentCore capabilities should be re-verified against current AWS documentation at implementation time (AgentCore is a fast-moving 2026 service); do not hardcode a stale capability list without checking it first.
- No alarm list exists yet for the admin email `deploy.sh` collects (B50/B75 resolve *what* alarms exist, but wiring them is F20) — this phase only collects and stores the admin email value.

## Handoff notes

- F2 (Hosting + auth) is the first phase to add real product resources to the stack this phase created; it should add Cognito/S3/CloudFront/WAF constructs to `backend-stack.ts` (or the equivalent stack file this phase settles on) using the Region and config values this phase's loader exposes.
- Every later phase's CDK code must follow this phase's conventions: cdk-nag stays stack-scoped, every suppression uses the ADR-format reason string, and Region comes from the config loader, never hardcoded.
- If this phase splits the CodeBuild/pipeline construct into a separate stack file from the product stack, record that choice clearly here so F2+ don't duplicate or misplace resources.
