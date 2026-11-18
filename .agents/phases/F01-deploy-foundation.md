# F01 — Deploy foundation

Status: done (sandbox `deploy.sh` run passed, verified by the user on 2026-10-05) | Depends on: — | Decisions: B45, B46, B47, B48, B52, B87 | Specs: [`../specs/architecture.md`](../specs/architecture.md) (Deploy foundation row, §1 component table), [`../specs/security.md`](../specs/security.md) (Infrastructure as code hygiene, Encryption)

**B111 (2026-11-18) replaced this phase's bash prompts and flags with a Python Textual TUI** (`deploy/cei_deploy`, launched by a thin `deploy.sh` wrapper). The plan and tasks below describe the original bash implementation; see `docs/deploymentGuide.md` and `.agents/decision-log.md` B111 for the current flow.

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

## Implementation plan (approved 2026-09-29, decision B87)

Choices (user-approved):
- `deploy.sh` creates the CodeBuild IAM role and project with the AWS CLI (no CDK pipeline stack). CodeBuild clones the GitHub repo URL + branch the user gives. For a private repo, the user gives a GitHub PAT; `deploy.sh` stores it as a CodeBuild source credential (never on disk, never in `cei.deploy.json`, never echoed). `BackendStack` stays the only product stack; F2+ add resources to it.
- The Region pre-flight is TypeScript. `deploy.sh` runs it locally before CodeBuild starts (fail fast). `buildspec.yml` runs it again in `pre_build` before `cdk deploy`.
- The pre-flight uses SSM public service parameters (`bedrock-agentcore`, `s3vectors`, `bedrock`) as a coarse gate, then a hardcoded feature matrix (doc URL + check date per entry). An endpoint probe cannot detect feature gaps. AgentCore Memory is not in us-west-1, eu-south-1, eu-south-2, ap-southeast-5, ap-south-2, ap-southeast-7 (checked 2026-09-29, https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-regions.html).
- Model check: `GetFoundationModelAvailability` (all of `regionAvailability`, `authorizationStatus`, `entitlementAvailability`, `agreementAvailability.status` must pass); for inference profiles also `GetInferenceProfile` → `status=ACTIVE`.
- Web Search Regions: us-east-1, eu-west-1, ap-northeast-1. Elsewhere `webSearchEnabled: false`, no failure.
- The CodeBuild project is not in CDK, so cdk-nag CodeBuild rules do not apply; the `cic-security` pass reviews its IAM role instead. The phase's "CDK assertion tests for the CodeBuild project" are replaced by stack tests for the config-aware skeleton.

Files:
1. `backend/lib/config/deploy-config.ts` — `DeployConfig` type + `loadDeployConfig()`; reads `cei.deploy.json` (path from context `ceiConfigFile`); validates Region, admin email, email domains, model IDs, limits (B22 defaults 20 rps / 2 real-time / 1 background); reports all errors at once; no new dependency.
2. `backend/lib/config/region-capabilities.ts` — pure feature matrix + `evaluateRegion(region, probeResults)` → `{ errors[], webSearchEnabled }`.
3. `backend/scripts/preflight.ts` — AWS SDK v3 probes, calls `evaluateRegion`. CLI: `npx ts-node scripts/preflight.ts --config cei.deploy.json [--write]`; exit 0/1; errors on stderr; `--write` sets `webSearchEnabled`.
4. `deploy.sh` (repo root) — prompts/flags; writes config; `npm ci`; pre-flight; `cdk bootstrap` if `CDKToolkit` is missing; create/update IAM role `cei-deploy-codebuild-role` (least privilege) and the CodeBuild project; optional GitHub PAT; `start-build`; poll; exit with build result. Flags `--non-interactive`, `--yes`, `--dry-run`.
5. `buildspec.yml` (repo root) — decode `CEI_DEPLOY_CONFIG` (base64) → `backend/cei.deploy.json`; pre-flight; build; test; `cdk deploy --require-approval never`.
6. `backend/bin/backend.ts` — Region from config; environment-agnostic with a warning if no config file.
7. `backend/lib/backend-stack.ts` — `BackendStackProps` with optional `config` (readonly for F2+), stack tags, `WebSearchEnabled` output; Code Interpreter and cdk-nag scope unchanged.
8. Tests under `backend/test/` — config loader, region evaluation, stack (Code Interpreter present, 0 cdk-nag errors).
9. `.gitignore` — `backend/cei.deploy.json`.
10. Docs — this file, decision log B87, `docs/deploymentGuide.md`, `docs/provisioned-resources.md`.

## Tasks

- [x] 1. Write `backend/lib/config/deploy-config.ts` (or similar): a typed loader for the values `deploy.sh` collects (Region, model blocklist, limits, email allowlist, admin email), reading from a CDK context JSON file `deploy.sh` writes.
- [x] 2. Write `deploy.sh`: prompt for (or accept via flags) target Region, model blocklist entries, per-user limits (B22 defaults — see F3/S2 for where these are enforced), allowed email domains (B16), and admin alert email (B50); write them to the CDK context file from task 1.
- [x] 3. Add a Region-capability precheck to `deploy.sh` (or a small companion script it calls) that verifies the selected Region has AgentCore Runtime, Gateway, Memory, and Policy; S3 Vectors; Bedrock Knowledge Base; and the configured default/allowed Bedrock models — fail with a clear message before CDK deploy starts if any are missing (per B45's requirement).
- [x] 4. Add region-gating for the AgentCore Web Search connector (B44: only us-east-1, eu-west-1, ap-northeast-1) to the same precheck — outside those Regions, `deploy.sh` must record "web search disabled" in the config rather than failing (web search itself is F12 scope; this phase only wires the config flag).
- [x] 5. Write `buildspec.yml` and the CodeBuild project construct in `backend-stack.ts` (or a separate `deploy-pipeline-stack.ts`, if keeping the pipeline construct out of the product stack is cleaner — note the choice made in this phase file's Handoff notes so F2+ know which stack to add resources to). **Done in a different form (B87):** `buildspec.yml` exists as planned, but the CodeBuild project is not a CDK construct at all — `deploy.sh` creates the IAM role and the CodeBuild project directly with the AWS CLI. There is no pipeline stack. See Handoff notes.
- [x] 6. Update `backend/bin/backend.ts` to pass the Region from the config loader into the stack's `env`, replacing today's fully-commented-out block.
- [x] 7. Confirm existing cdk-nag `AwsSolutionsChecks` scoping (stack-level, not app-level — the existing ADR in `backend-stack.ts`) still applies once the CodeBuild/pipeline construct is added; add any new suppressions needed, each with the ADR-format reason string (`CLAUDE.md` convention). No new suppression was needed: the CodeBuild project and its IAM role are outside CDK, so cdk-nag does not see them (see Implementation plan note on this).
- [x] 8. Run `npx cdk synth` and `npm test` in `backend/` and confirm the skeleton still synthesizes cleanly with the existing Code Interpreter resource plus the new config/pipeline additions.

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

- There is no separate pipeline stack. `deploy.sh` creates the CodeBuild IAM role (`cei-deploy-codebuild-role`) and the CodeBuild project (`cei-life-sciences-deploy`) directly with the AWS CLI, outside CDK. F2+ add every new product resource to `BackendStack` in `backend/lib/backend-stack.ts` — do not create a new stack file for them.
- F2 (Hosting + auth) is the first phase to add real product resources to `BackendStack`; it should add Cognito/S3/CloudFront/WAF constructs there, using the Region and config values `stack.config` exposes.
- Read config through `stack.config` (the `DeployConfig` type in `backend/lib/config/deploy-config.ts`), never by re-reading `cei.deploy.json` directly in a construct.
- Region comes from config only (`config.region`, passed into `env` in `backend/bin/backend.ts`) — never hardcode a Region in later phases' CDK code.
- `config.webSearchEnabled` is wired now but only F12 (Web search + LS ops) needs to read it; earlier phases can ignore it.
- Every later phase's CDK code must follow this phase's conventions: cdk-nag stays stack-scoped, every suppression uses the ADR-format reason string, and Region comes from the config loader, never hardcoded.
- Adding a new config field (for a later phase) touches three places together: `parseDeployConfig` in `backend/lib/config/deploy-config.ts`, the matching prompt/flag in `deploy.sh`, and the Jest tests for both.
- **B91 (2026-10-01) supersedes part of this phase's Implementation plan above.** `deploy.sh` no longer runs the pre-flight locally and no longer runs `npm ci` or `cdk bootstrap` itself. All build steps, including the pre-flight (`--write`) and `cdk bootstrap`, run only inside CodeBuild (`buildspec.yml`). `deploy.sh` only collects configuration, does a read-only `CDKToolkit` check to decide whether the CodeBuild role needs the one-time `cei-deploy-bootstrap` policy, and starts/polls the build. The bullets above are kept as a record of the original plan; see B91 in `.agents/decision-log.md` and `docs/deploymentGuide.md` for the current flow.
