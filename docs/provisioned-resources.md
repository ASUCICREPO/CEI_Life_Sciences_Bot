# Provisioned Resources — CEI Life Sciences Bot

This page lists only what is **really deployed today** — through `backend/lib/backend-stack.ts` (CDK) and through `deploy.sh` (AWS CLI, outside CDK). For the full target architecture, and everything that is planned but not yet built, see [Architecture Deep Dive](./architectureDeepDive.md) and `.agents/specs/architecture.md`.

---

## What is deployed by CDK

Two stacks now exist: `BackendStack` (`backend/lib/backend-stack.ts`, deploys to the configured Region) and `EdgeWafStack` (`backend/lib/edge-waf-stack.ts`, always deploys to `us-east-1`, B88). `BackendStack` reads `EdgeWafStack`'s web ACL ARN via `crossRegionReferences: true`.

### `BackendStack`

| Resource | Service | Status | Notes |
|---|---|---|---|
| Code Interpreter (`ceiLifeSciencesCI`) | AgentCore Code Interpreter | Provisioned via CDK | `CodeInterpreterCustom` construct; public network mode (needed for calls to external database APIs and Python package installs) |
| `CodeInterpreterId` stack output | CloudFormation output | Provisioned via CDK | Exported as `CeiCodeInterpreterId`; intended to be passed to future runtimes/Lambdas as `CEI_CODE_INTERPRETER_ID` |
| `WebSearchEnabled` stack output | CloudFormation output | Provisioned via CDK, only when a deploy config is present | Set by `backend/scripts/preflight.ts` from the Region (`true` only in us-east-1, eu-west-1, ap-northeast-1); read at build time by later phases that wire the Web Search connector (F12) |
| Pre-sign-up domain check Lambda | AWS Lambda (Python 3.14) | Provisioned via CDK | `PreSignUpDomainCheckFn`; own log group, own hand-rolled role (log permissions only, no `AWSLambdaBasicExecutionRole`); env var `ALLOWED_EMAIL_DOMAINS` from the deploy config (B16) |
| Cognito User Pool | Amazon Cognito | Provisioned via CDK | Self sign-up, email sign-in, auto-verify email, `AccountRecovery.EMAIL_ONLY`, MFA off (B14), password policy (min 12 chars, upper/lower/digit/symbol), the pre-sign-up trigger above attached, `deletionProtection: true`, `RemovalPolicy.RETAIN` |
| Cognito User Pool domain + managed login branding | Amazon Cognito | Provisioned via CDK | Prefix domain `cei-ls-<account-id>`; `CfnManagedLoginBranding` with `useCognitoProvidedValues: true` (B89) |
| Cognito SPA app client | Amazon Cognito | Provisioned via CDK | `SpaClient`; no secret, Authorization Code + PKCE only, scopes `openid email profile`, callback/logout URLs = the CloudFront domain (plus `localhost:5173` when `config.allowLocalhostCallback`, B90); access/ID tokens 60 min, refresh token 7 days |
| Access-logs bucket | Amazon S3 | Provisioned via CDK | Private, `OBJECT_WRITER` ownership (required for classic S3/CloudFront log delivery ACLs), SSL-only, S3-managed encryption, `RemovalPolicy.RETAIN` |
| Site bucket | Amazon S3 | Provisioned via CDK | Private (`BLOCK_ALL`), SSL-only, S3-managed encryption, server access logs to the access-logs bucket, `RemovalPolicy.RETAIN` |
| Response headers policy | Amazon CloudFront | Provisioned via CDK | `SecurityHeadersPolicy`: baseline CSP, HSTS, `X-Content-Type-Options`, frame `DENY`, strict referrer policy |
| CloudFront distribution | Amazon CloudFront | Provisioned via CDK | `S3BucketOrigin.withOriginAccessControl` over the site bucket; `defaultRootObject: index.html`; 403/404 → `/index.html` (HTTP 200) for SPA client-side routing; standard access logging to the access-logs bucket; `webAclId` from `EdgeWafStack` |
| `SiteAssetsDeployment` / `RuntimeConfigDeployment` | S3 bucket deployment (`aws-s3-deployment`) | Provisioned via CDK | Two `BucketDeployment`s in order: the built SPA (`frontendAssetPath`, resolved to `../frontend/dist`), then `/config.json` (`Source.jsonData`, `Cache-Control: no-cache`, `prune: false`) with a distribution invalidation on each |
| `UserPoolId`, `UserPoolArn`, `UserPoolClientId`, `CognitoDomain`, `DistributionDomainName`, `SiteUrl` stack outputs | CloudFormation outputs | Provisioned via CDK | For F3's REST API authorizer and the SPA runtime config |

### `EdgeWafStack` (always `us-east-1`, B88)

| Resource | Service | Status | Notes |
|---|---|---|---|
| `EdgeWebAcl` (`cei-life-sciences-edge-waf`) | AWS WAFv2 | Provisioned via CDK | `CLOUDFRONT`-scope `CfnWebACL`; `AWSManagedRulesCommonRuleSet`, `AWSManagedRulesKnownBadInputsRuleSet`, `AWSManagedRulesAmazonIpReputationList`, and a rate-based rule (1000 requests / 5 min per IP — a proposal, values TBD per `security.md` T5); CloudWatch metrics on for every rule |
| `WebAclArn` stack output | CloudFormation output | Provisioned via CDK | Consumed by `BackendStack` through `crossRegionReferences: true` |

No IAM role other than the pre-sign-up Lambda's, no DynamoDB table, no API Gateway, and no SQS queue is defined in the CDK code yet.

## What `deploy.sh` creates outside CDK (F1, B87)

`deploy.sh` sets up the deployment pipeline itself with the AWS CLI, not with CDK. These resources exist once `deploy.sh` has been run at least once against an account/Region:

| Resource | Service | Status | Notes |
|---|---|---|---|
| `cei-deploy-codebuild-role` | IAM role | Created/updated by `deploy.sh` | Tagged `cei:owner=deploy.sh`; trust policy requires `aws:SourceAccount` and `aws:SourceArn` (the `cei-life-sciences-deploy` project ARN) to match; inline policy `cei-deploy-codebuild-policy` scopes CDK bootstrap role assumption to `cdk-hnb659fds-*-<account>-<region>` **and**, when the target Region is not `us-east-1`, to the matching `us-east-1` bootstrap roles too (needed for `EdgeWafStack`, B88); also grants the CDK bootstrap SSM parameter read (in both Regions, same condition), Bedrock model-availability checks, its own CloudWatch Logs group, and (only if a PAT secret exists) reading that one secret |
| `cei-life-sciences-deploy` | CodeBuild project | Created/updated by `deploy.sh` | Tagged `cei:owner=deploy.sh`; source: the given GitHub repository URL (normalized to `https://github.com/<owner>/<repo>`, no embedded credentials) and branch; buildspec `buildspec.yml`; image `aws/codebuild/standard:8.0`; each build is started pinned to a resolved commit SHA (`start-build --source-version`), not just the branch; runs `cdk deploy --all` for both `BackendStack` and `EdgeWafStack` |
| `cei/deploy/github-pat` | Secrets Manager secret | Created only if the user supplies a GitHub PAT for a private repository | Tagged `cei:owner=deploy.sh`; attached to the `cei-life-sciences-deploy` project only, through the project's own `source.auth` field (`SECRETS_MANAGER`) — never set as the account-wide default GitHub credential |
| `CDKToolkit` | CloudFormation stack (CDK bootstrap) | Created by `deploy.sh` only if not already present | Standard `cdk bootstrap` stack, in the target Region; also bootstrapped in `us-east-1` if the target Region is not `us-east-1` (B88 — `EdgeWafStack` always deploys there); not part of `BackendStack` |

`deploy.sh` checks the `cei:owner=deploy.sh` tag on any same-named existing resource before updating it, and stops with an error instead of modifying or overwriting a resource it does not own.

---

## What exists outside CDK (not AWS-provisioned)

These run locally today and are not deployed AWS resources:

- **Research and Drafting agent graphs** (`backend/agents/research/`, `backend/agents/drafting/`) — run locally via the AgentCore CLI, using `InMemorySaver` as the checkpointer (not `AgentCoreMemorySaver`).
- **Analysis and Pipeline Manager agent graphs** — wired, but every node raises `NotImplementedError`.
- **Data source calls** (`backend/agents/shared/tools/data_sources/`) — direct HTTP calls to PubMed, ClinicalTrials.gov, bioRxiv, medRxiv, ChEMBL, Open Targets, CMS Coverage, ICD-10, NPI Registry, and PubChem. No AgentCore Gateway, no Lambda deployment.
- **CEI UI 1.1 presenters and schemas** (`backend/agents/shared/presenters/`, `backend/agents/shared/schemas/`) — produce completed JSON `ui` surfaces locally.
- **Frontend** (`frontend/`) — the `founder-ai-dashboard` Vite SPA (React 19, Tailwind 4, react-router), the production frontend (B1/B66). It is built and deployed to the CDK-provisioned S3 bucket and served through CloudFront (see the `BackendStack` table above). The archived Next.js test harness and its local proxy routes/CEI UI preview renderer now live in `archive/frontend-next/` and are not deployed. The NCBI Vite dev proxy in `frontend/` is still in place (B21 removes it in F15).

## What is not deployed and not built at all

Everything else in the target architecture — the REST API Gateway, the Session Runner and Session Router Lambdas, the AgentCore Gateway and Cedar Policy engine, the tool-agent runtime, DynamoDB tables (Session Metadata, Chat History, Job ID, User Metadata, Attachment Metadata), the Attachments S3 bucket, S3 Vectors, the product API Key Store in Secrets Manager, SQS, Bedrock Knowledge Base, Bedrock Guardrails, AgentCore Memory (short- and long-term), the Web Search connector, the Life Sciences Operations Lambda, and CloudWatch alarms/SNS — is **planned, not built**. (The one Secrets Manager secret that does exist, `cei/deploy/github-pat`, belongs to the deploy mechanism, not the product — see the table above.) See [Architecture Deep Dive](./architectureDeepDive.md) for the full component table, status per component, and `.agents/specs/architecture.md` for detail.

---

## Planned resources

For the target list of every AWS resource this project will provision, with phase and status, see:

- [Architecture Deep Dive](./architectureDeepDive.md) — human-readable component table
- `.agents/specs/architecture.md` — full component table with build phase per resource
- `.agents/decision-log.md` — the decisions behind each resource choice
