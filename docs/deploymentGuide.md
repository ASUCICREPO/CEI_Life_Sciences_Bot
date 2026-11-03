# Deployment Guide

Status: **Planned — not built.** This page is an outline of the planned deployment flow, not step-by-step instructions. There is no `deploy.sh`, no CodeBuild pipeline, and no deployed frontend hosting yet. Today, only the AgentCore Code Interpreter can be deployed, by running `cdk deploy` directly in `backend/` (see [Provisioned Resources](./provisioned-resources.md)). This guide will become step-by-step once the features it describes exist. Full detail: `.agents/specs/architecture.md`, `.agents/decision-log.md` (B45–B48).

---

## Planned deployment model

- **Single environment.** No separate dev/staging/prod stacks. Developers use the local AgentCore CLI for agent development before anything is deployed.
- **Region is configurable**, through environment variables and CDK context — there is no fixed Region.
- **All AgentCore resources go into CDK**: runtimes, Gateway and targets, Memory, Policy engine, Knowledge Base. `agentcore.json` stays local-development-only.

---

## Planned: `deploy.sh` guided setup

A guided shell script will collect configuration, then start a CodeBuild deployment. Planned prompts:

| Setting | Purpose |
|---|---|
| Repository | Where the source lives for CodeBuild to pull from |
| Region | Deployment Region — the script checks the Region has every required service (AgentCore Runtime/Gateway/Memory/Policy, S3 Vectors, Bedrock Knowledge Base, the selected models) before continuing |
| Model blocklist / allowlist | Which Bedrock model IDs users may never select |
| Usage limits | API Gateway throttle, per-user concurrency limits |
| Allowed email domains | Cognito pre-sign-up domain allowlist |
| Admin alarm email | Where CloudWatch/SNS alarm notifications go |

After collecting this configuration, `deploy.sh` starts a CodeBuild project that runs the actual CDK deployment. The goal is a guided, low-friction deployment for a single environment — not a multi-stage pipeline.

---

## Planned: backend deployment

1. `deploy.sh` writes the collected configuration (Region, model blocklist, limits, email domains, admin email) into CDK context or environment variables.
2. CodeBuild runs `cdk deploy` for the full stack: Cognito, the REST API Gateway, Session Runner/Router Lambdas, AgentCore Gateway and Policy, AgentCore Runtimes, DynamoDB tables, S3 buckets, SQS, Secrets Manager, and CloudWatch alarms/SNS.
3. Stack outputs (API endpoint, Cognito pool/client IDs, CloudFront domain) feed the frontend build.

Today, only `cdk deploy` for the Code Interpreter construct exists; none of the above resources are defined in `backend/lib/backend-stack.ts` yet.

---

## Planned: frontend deployment

The production frontend target is a static build of the Vite dashboard (replacing the current Next.js `frontend/` test harness — see [Architecture Deep Dive](./architectureDeepDive.md), B1/B66), served from **S3 + CloudFront**. There is no Vercel or Amplify deployment target for production; those options are removed from this project's plan.

Planned steps: build the static SPA, upload it to the S3 bucket created by CDK, and invalidate the CloudFront distribution. CloudFront needs an SPA fallback (403/404 → `index.html`) for client-side routing.

---

## Planned: post-deployment verification

Once the above exists, verification will include: checking the CloudFormation stack status, confirming the Cognito pool accepts sign-ups from allowed email domains, testing `/chat/stream` end to end, and confirming CloudWatch alarms reach the admin email.

---

## Cleanup

To remove the resource that exists today:

```bash
cd backend
cdk destroy
```

> **Warning:** this deletes the Code Interpreter construct and its CloudFormation output. Once the full stack exists, `cdk destroy` will remove all deployed resources — confirm nothing important is only stored there before running it.

---

## Next steps

- [Architecture Deep Dive](./architectureDeepDive.md) — target architecture and current status per component
- [Provisioned Resources](./provisioned-resources.md) — what is really deployed today
- `.agents/specs/architecture.md` — full target architecture spec
- `.agents/decision-log.md` — decisions behind the deployment design (B45–B48)
