# Provisioned Resources — CEI Life Sciences Bot

This page lists only what is **really deployed today** — through `backend/lib/backend-stack.ts` (CDK) and through `deploy.sh` (AWS CLI, outside CDK). For the full target architecture, and everything that is planned but not yet built, see [Architecture Deep Dive](./architectureDeepDive.md) and `.agents/specs/architecture.md`.

---

## What is deployed by CDK (`backend/lib/backend-stack.ts`)

| Resource | Service | Status | Notes |
|---|---|---|---|
| Code Interpreter (`ceiLifeSciencesCI`) | AgentCore Code Interpreter | Provisioned via CDK | `CodeInterpreterCustom` construct in `backend/lib/backend-stack.ts`; public network mode (needed for calls to external database APIs and Python package installs) |
| `CodeInterpreterId` stack output | CloudFormation output | Provisioned via CDK | Exported as `CeiCodeInterpreterId`; intended to be passed to future runtimes/Lambdas as `CEI_CODE_INTERPRETER_ID` |
| `WebSearchEnabled` stack output | CloudFormation output | Provisioned via CDK, only when a deploy config is present | Set by `backend/scripts/preflight.ts` from the Region (`true` only in us-east-1, eu-west-1, ap-northeast-1); read at build time by later phases that wire the Web Search connector (F12) |

That is the entire content of the CDK stack today. No IAM role, Lambda, API Gateway, Cognito pool, DynamoDB table, S3 bucket, or SQS queue is defined in the CDK code.

## What `deploy.sh` creates outside CDK (F1, B87)

`deploy.sh` sets up the deployment pipeline itself with the AWS CLI, not with CDK. These resources exist once `deploy.sh` has been run at least once against an account/Region:

| Resource | Service | Status | Notes |
|---|---|---|---|
| `cei-deploy-codebuild-role` | IAM role | Created/updated by `deploy.sh` | Tagged `cei:owner=deploy.sh`; trust policy requires `aws:SourceAccount` and `aws:SourceArn` (the `cei-life-sciences-deploy` project ARN) to match; inline policy `cei-deploy-codebuild-policy` scopes CDK bootstrap role assumption to `cdk-hnb659fds-*-<account>-<region>`, plus the CDK bootstrap SSM parameter, Bedrock model-availability checks, its own CloudWatch Logs group, and (only if a PAT secret exists) reading that one secret |
| `cei-life-sciences-deploy` | CodeBuild project | Created/updated by `deploy.sh` | Tagged `cei:owner=deploy.sh`; source: the given GitHub repository URL (normalized to `https://github.com/<owner>/<repo>`, no embedded credentials) and branch; buildspec `buildspec.yml`; image `aws/codebuild/standard:8.0`; each build is started pinned to a resolved commit SHA (`start-build --source-version`), not just the branch; runs `cdk deploy` for `BackendStack` |
| `cei/deploy/github-pat` | Secrets Manager secret | Created only if the user supplies a GitHub PAT for a private repository | Tagged `cei:owner=deploy.sh`; attached to the `cei-life-sciences-deploy` project only, through the project's own `source.auth` field (`SECRETS_MANAGER`) — never set as the account-wide default GitHub credential |
| `CDKToolkit` | CloudFormation stack (CDK bootstrap) | Created by `deploy.sh` only if not already present in the target Region | Standard `cdk bootstrap` stack; not part of `BackendStack` |

`deploy.sh` checks the `cei:owner=deploy.sh` tag on any same-named existing resource before updating it, and stops with an error instead of modifying or overwriting a resource it does not own.

---

## What exists outside CDK (not AWS-provisioned)

These run locally today and are not deployed AWS resources:

- **Research and Drafting agent graphs** (`backend/agents/research/`, `backend/agents/drafting/`) — run locally via the AgentCore CLI, using `InMemorySaver` as the checkpointer (not `AgentCoreMemorySaver`).
- **Analysis and Pipeline Manager agent graphs** — wired, but every node raises `NotImplementedError`.
- **Data source calls** (`backend/agents/shared/tools/data_sources/`) — direct HTTP calls to PubMed, ClinicalTrials.gov, bioRxiv, medRxiv, ChEMBL, Open Targets, CMS Coverage, ICD-10, NPI Registry, and PubChem. No AgentCore Gateway, no Lambda deployment.
- **CEI UI 1.1 presenters and schemas** (`backend/agents/shared/presenters/`, `backend/agents/shared/schemas/`) — produce completed JSON `ui` surfaces locally.
- **Frontend** (`frontend/`) — a Next.js 16 test harness with local proxy routes (`CEI_ENABLE_LOCAL_PROXY=true`) and a CEI UI preview renderer. It is not deployed to S3/CloudFront and is not the planned production frontend (see the Vite dashboard plan in [Architecture Deep Dive](./architectureDeepDive.md)).

## What is not deployed and not built at all

Everything else in the target architecture — Cognito, the REST API Gateway, the Session Runner and Session Router Lambdas, the AgentCore Gateway and Cedar Policy engine, the tool-agent runtime, DynamoDB tables (Session Metadata, Chat History, Job ID, User Metadata, Attachment Metadata), S3 buckets (Attachments, static frontend, S3 Vectors), the product API Key Store in Secrets Manager, SQS, Bedrock Knowledge Base, Bedrock Guardrails, AgentCore Memory (short- and long-term), the Web Search connector, the Life Sciences Operations Lambda, and CloudWatch alarms/SNS — is **planned, not built**. (The one Secrets Manager secret that does exist, `cei/deploy/github-pat`, belongs to the deploy mechanism, not the product — see the table above.) See [Architecture Deep Dive](./architectureDeepDive.md) for the full component table, status per component, and `.agents/specs/architecture.md` for detail.

---

## Planned resources

For the target list of every AWS resource this project will provision, with phase and status, see:

- [Architecture Deep Dive](./architectureDeepDive.md) — human-readable component table
- `.agents/specs/architecture.md` — full component table with build phase per resource
- `.agents/decision-log.md` — the decisions behind each resource choice
