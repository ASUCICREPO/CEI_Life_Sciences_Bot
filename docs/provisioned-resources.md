# Provisioned Resources — CEI Life Sciences Bot

This page lists only what is **really deployed today** through `backend/lib/backend-stack.ts`. For the full target architecture, and everything that is planned but not yet built, see [Architecture Deep Dive](./architectureDeepDive.md) and `.agents/specs/architecture.md`.

---

## What is deployed

| Resource | Service | Status | Notes |
|---|---|---|---|
| Code Interpreter (`ceiLifeSciencesCI`) | AgentCore Code Interpreter | Provisioned via CDK | `CodeInterpreterCustom` construct in `backend/lib/backend-stack.ts`; public network mode (needed for calls to external database APIs and Python package installs) |
| `CodeInterpreterId` stack output | CloudFormation output | Provisioned via CDK | Exported as `CeiCodeInterpreterId`; intended to be passed to future runtimes/Lambdas as `CEI_CODE_INTERPRETER_ID` |

That is the entire content of the CDK stack today. No IAM role, Lambda, API Gateway, Cognito pool, DynamoDB table, S3 bucket, SQS queue, or CloudWatch resource is defined in this repository's infrastructure code.

---

## What exists outside CDK (not AWS-provisioned)

These run locally today and are not deployed AWS resources:

- **Research and Drafting agent graphs** (`backend/agents/research/`, `backend/agents/drafting/`) — run locally via the AgentCore CLI, using `InMemorySaver` as the checkpointer (not `AgentCoreMemorySaver`).
- **Analysis and Pipeline Manager agent graphs** — wired, but every node raises `NotImplementedError`.
- **Data source calls** (`backend/agents/shared/tools/data_sources/`) — direct HTTP calls to PubMed, ClinicalTrials.gov, bioRxiv, medRxiv, ChEMBL, Open Targets, CMS Coverage, ICD-10, NPI Registry, and PubChem. No AgentCore Gateway, no Lambda deployment.
- **CEI UI 1.1 presenters and schemas** (`backend/agents/shared/presenters/`, `backend/agents/shared/schemas/`) — produce completed JSON `ui` surfaces locally.
- **Frontend** (`frontend/`) — a Next.js 16 test harness with local proxy routes (`CEI_ENABLE_LOCAL_PROXY=true`) and a CEI UI preview renderer. It is not deployed to S3/CloudFront and is not the planned production frontend (see the Vite dashboard plan in [Architecture Deep Dive](./architectureDeepDive.md)).

## What is not deployed and not built at all

Everything else in the target architecture — Cognito, the REST API Gateway, the Session Runner and Session Router Lambdas, the AgentCore Gateway and Cedar Policy engine, the tool-agent runtime, DynamoDB tables (Session Metadata, Chat History, Job ID, User Metadata, Attachment Metadata), S3 buckets (Attachments, static frontend, S3 Vectors), Secrets Manager, SQS, Bedrock Knowledge Base, Bedrock Guardrails, AgentCore Memory (short- and long-term), the Web Search connector, the Life Sciences Operations Lambda, and CloudWatch alarms/SNS — is **planned, not built**. See [Architecture Deep Dive](./architectureDeepDive.md) for the full component table, status per component, and `.agents/specs/architecture.md` for detail.

---

## Planned resources

For the target list of every AWS resource this project will provision, with phase and status, see:

- [Architecture Deep Dive](./architectureDeepDive.md) — human-readable component table
- `.agents/specs/architecture.md` — full component table with build phase per resource
- `.agents/decision-log.md` — the decisions behind each resource choice
