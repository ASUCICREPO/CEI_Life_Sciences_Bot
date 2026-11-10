# Provisioned Resources — CEI Life Sciences Bot

This page lists only what is **really deployed today** — through `backend/lib/backend-stack.ts` (CDK, run inside a CodeBuild build) and through `deploy.sh` itself (AWS CLI, outside CDK). All build steps (`npm ci`, the Region pre-flight, the frontend build, `cdk bootstrap`, `cdk deploy`) run in CodeBuild, not in `deploy.sh` (B91) — see [Deployment Guide](./deploymentGuide.md). For the full target architecture, and everything that is planned but not yet built, see [Architecture Deep Dive](./architectureDeepDive.md) and `.agents/specs/architecture.md`.

---

## What is deployed by CDK

Two stacks now exist: `BackendStack` (`backend/lib/backend-stack.ts`, deploys to the configured Region) and `EdgeWafStack` (`backend/lib/edge-waf-stack.ts`, always deploys to `us-east-1`, B88). `BackendStack` reads `EdgeWafStack`'s web ACL ARN via `crossRegionReferences: true`.

### `BackendStack`

| Resource | Service | Status | Notes |
|---|---|---|---|
| Code Interpreter (`ceiLifeSciencesCI`) | AgentCore Code Interpreter | Provisioned via CDK | `CodeInterpreterCustom` construct; public network mode (needed for calls to external database APIs and Python package installs) |
| `CodeInterpreterId` stack output | CloudFormation output | Provisioned via CDK | Exported as `CeiCodeInterpreterId`; the four agent runtimes get it as `CEI_CODE_INTERPRETER_ID` and have use of the Code Interpreter (`grantUse`) |
| `WebSearchEnabled` stack output | CloudFormation output | Provisioned via CDK, only when a deploy config is present | Set by `backend/scripts/preflight.ts` from the Region (`true` only in us-east-1, eu-west-1, ap-northeast-1); read at build time by later phases that wire the Web Search connector (F12) |
| Pre-sign-up domain check Lambda | AWS Lambda (Python 3.14) | Provisioned via CDK | `PreSignUpDomainCheckFn`; own log group, own hand-rolled role (log permissions only, no `AWSLambdaBasicExecutionRole`); env var `ALLOWED_EMAIL_DOMAINS` from the deploy config (B16) |
| Cognito User Pool | Amazon Cognito | Provisioned via CDK | Self sign-up, email sign-in, auto-verify email, `AccountRecovery.EMAIL_ONLY`, MFA off (B14), password policy (min 12 chars, upper/lower/digit/symbol), the pre-sign-up trigger above attached, `deletionProtection: true`, `RemovalPolicy.RETAIN` |
| Cognito User Pool domain + managed login branding | Amazon Cognito | Provisioned via CDK | Prefix domain `cei-ls-<account-id>`; `CfnManagedLoginBranding` with `useCognitoProvidedValues: true` (B89) |
| Cognito SPA app client | Amazon Cognito | Provisioned via CDK | `SpaClient`; no secret, Authorization Code + PKCE only, scopes `openid email profile`, callback/logout URLs = the CloudFront domain (plus `localhost:5173` when `config.allowLocalhostCallback`, B90); access/ID tokens 60 min, refresh token 7 days |
| Access-logs bucket | Amazon S3 | Provisioned via CDK | Private, `OBJECT_WRITER` ownership (required for classic S3/CloudFront log delivery ACLs), SSL-only, S3-managed encryption, `RemovalPolicy.RETAIN` |
| Site bucket | Amazon S3 | Provisioned via CDK | Private (`BLOCK_ALL`), SSL-only, S3-managed encryption, server access logs to the access-logs bucket, `RemovalPolicy.RETAIN` |
| Response headers policy | Amazon CloudFront | Provisioned via CDK | `SecurityHeadersPolicy`: baseline CSP, HSTS, `X-Content-Type-Options`, frame `DENY`, strict referrer policy |
| CloudFront distribution | Amazon CloudFront | Provisioned via CDK | `S3BucketOrigin.withOriginAccessControl` over the site bucket; `defaultRootObject: index.html`; SPA routing uses the CloudFront Function `SpaFallback` on the default (S3) behavior only: a viewer-request path with no `.` becomes `/index.html` (there are no distribution-wide 403/404 rewrites, so API errors stay API errors); the `chat/stream` behavior goes to the REST API origin (`/v1`) with caching off; standard access logging to the access-logs bucket; `webAclId` from `EdgeWafStack` |
| `SiteAssetsDeployment` / `RuntimeConfigDeployment` | S3 bucket deployment (`aws-s3-deployment`) | Provisioned via CDK | Two `BucketDeployment`s in order: the built SPA (`frontendAssetPath`, resolved to `../frontend/dist`), then `/config.json` (`Source.jsonData`, `Cache-Control: no-cache`, `prune: false`) with a distribution invalidation on each |
| Attachments bucket | Amazon S3 | Provisioned via CDK | `AttachmentsBucket`; private, `RemovalPolicy.RETAIN`; its name reaches the agents as `CEI_ATTACHMENTS_BUCKET` and is the `AttachmentsBucketName` output. The upload routes (F10) are not built |
| Exports bucket | Amazon S3 | Provisioned via CDK | `ExportsBucket`; private, `RemovalPolicy.RETAIN`; holds export files that `GET /exports/{exportId}/download` signs |
| REST API | Amazon API Gateway (REST, Regional) | Provisioned via CDK | `cei-life-sciences-api`, stage `v1`; Cognito authorizer (scope `openid`); stage throttle; access logs; X-Ray tracing. Routes: [API documentation](./APIDoc.md) |
| DynamoDB tables | Amazon DynamoDB | Provisioned via CDK | `ChatMetadataStore`, `ChatHistoryStore`, `ExportMetadataStore`, `ActiveRunsStore` (`backend-stack.ts`); `JobIdStore` (`constructs/background-jobs.ts`); `NcbiRateLimiter` (`constructs/tool-gateway.ts`) |
| Session API Lambdas | AWS Lambda (Python 3.14) | Provisioned via CDK | `SessionMetadataHandler`, `SessionHistoryHandler`, `ExportsHandler`, `ClientDiagnostics` |
| Agent proxy Lambda + function URL | AWS Lambda (Python 3.14) | Provisioned via CDK | `AgentProxyFn`; buffered function URL (`authType: NONE`; AgentCore checks the forwarded Cognito token); forwards to the runtime invocation URLs (`AGENT_URLS`) |
| Session Router Lambda | AWS Lambda (Python 3.14) | Provisioned via CDK | `constructs/session-router.ts`; deterministic routing only (the free-text classifier of F9 is not wired) |
| Session Runner Lambda | AWS Lambda (Node.js 24) | Provisioned via CDK | `SessionRunner`; serves `POST /chat/stream` in STREAM mode (14-minute integration timeout) |
| UI actions Lambda | AWS Lambda (Node.js 24) | Provisioned via CDK | `UiActions`; serves `POST /ui-actions` in STREAM mode |
| Background jobs | AWS Lambda + Amazon EventBridge | Provisioned via CDK | `JobsHandler` (the `/jobs` routes) and `JobWatchdog`, run by a 5-minute EventBridge rule (`constructs/background-jobs.ts`) |
| AgentCore Gateway + tool Lambdas | AgentCore Gateway, AWS Lambda | Provisioned via CDK | `constructs/tool-gateway.ts`: Gateway, `PolicyEngine` with one Cedar policy per target (mode from deploy config, default `LOG_ONLY`), the 12 data-source Lambdas, `NcbiReadOnly` targets, `PreTokenGeneration` trigger, `TokenRefresh` Lambda (`POST /internal/token-refresh`, `AWS_IAM`), the Router M2M secret (`RouterM2MSecret`), and Gateway log delivery |
| Life Sciences Operations Lambda | AWS Lambda (container image) + Gateway target | Provisioned via CDK | `constructs/life-sciences-operations.ts` (`lsops`), with its own Cedar policy |
| Agent runtimes | AgentCore Runtime | Provisioned via CDK | `CEIResearchAgent`, `CEIDraftingAgent`, `CEIAnalysisAgent`, `CEIPipelineManager` (`constructs/agent-runtimes.ts`); Cognito JWT authorizer; each runtime has use of the Code Interpreter |
| Agent memory | AgentCore Memory | Provisioned via CDK | `CEIResearchMemory` (`CfnMemory`), 365-day event expiry, long-term strategies `user_preferences` and `semantic_facts`; used by the Research runtime only; `RemovalPolicy.RETAIN` |
| Alarms, SNS topic, dashboard | Amazon CloudWatch, Amazon SNS | Provisioned via CDK | `constructs/observability.ts`: alert topic (email subscription when an admin email is set), Lambda error/throttle alarms, DynamoDB throttle alarms, API 5xx alarm, handled-failure alarms, and the `<stack>-operations` dashboard. See [Observability](./observability.md) |
| `UserPoolId`, `UserPoolArn`, `UserPoolClientId`, `CognitoDomain`, `DistributionDomainName`, `SiteUrl`, `ApiBaseUrl`, `AgentProxyUrl`, `AttachmentsBucketName`, runtime ARN stack outputs | CloudFormation outputs | Provisioned via CDK | For the SPA runtime config (`/config.json`) and operators |

### `EdgeWafStack` (always `us-east-1`, B88)

| Resource | Service | Status | Notes |
|---|---|---|---|
| `EdgeWebAcl` (`cei-life-sciences-edge-waf`) | AWS WAFv2 | Provisioned via CDK | `CLOUDFRONT`-scope `CfnWebACL`; `AWSManagedRulesCommonRuleSet`, `AWSManagedRulesKnownBadInputsRuleSet`, `AWSManagedRulesAmazonIpReputationList`, and a rate-based rule (1000 requests / 5 min per IP — a proposal, values TBD per `security.md` T5); CloudWatch metrics on for every rule |
| `WebAclArn` stack output | CloudFormation output | Provisioned via CDK | Consumed by `BackendStack` through `crossRegionReferences: true` |

No SQS queue, Bedrock Knowledge Base, S3 Vectors index, or Bedrock Guardrail is attached in the stack yet. (`constructs/model-guardrail.ts` exists but `BackendStack` does not use it.) `backend/lambdas/delete-worker/` is a tested foundation only; no CDK resource uses it.

## What `deploy.sh` creates outside CDK (F1, B87, B91)

`deploy.sh` sets up the deployment pipeline itself with the AWS CLI, not with CDK. `deploy.sh` runs no build step itself (no `npm ci`, no pre-flight, no `cdk bootstrap`) — it only creates/updates these pipeline resources, then starts and polls a CodeBuild build that runs every build step (B91). These resources exist once `deploy.sh` has been run at least once against an account/Region:

| Resource | Service | Status | Notes |
|---|---|---|---|
| `cei-deploy-codebuild-role` | IAM role | Created/updated by `deploy.sh` | Tagged `cei:owner=deploy.sh`; trust policy requires `aws:SourceAccount` and `aws:SourceArn` (the `cei-life-sciences-deploy` project ARN) to match; inline policy `cei-deploy-codebuild-policy` scopes CDK bootstrap role assumption to `cdk-hnb659fds-*-<account>-<region>` **and**, when the target Region is not `us-east-1`, to the matching `us-east-1` bootstrap roles too (needed for `EdgeWafStack`, B88); also grants `cloudformation:DescribeStacks` on `CDKToolkit` in both Regions, the CDK bootstrap SSM parameter read, Bedrock model-availability checks, its own CloudWatch Logs group, and (only if a PAT secret exists) reading that one secret. A separate inline policy, `cei-deploy-bootstrap` (CloudFormation on stack `CDKToolkit`; IAM on `cdk-hnb659fds-*` roles including `PassRole`/`AttachRolePolicy`; S3 on the `cdk-hnb659fds-assets` bucket; ECR on the container-assets repo; SSM on `/cdk-bootstrap/hnb659fds/*`), is attached only for a run where a Region still needs `CDKToolkit`, and removed again when no Region needs it (B91) — this is what lets the CodeBuild role, not `deploy.sh`'s own credentials, run `cdk bootstrap` |
| `cei-life-sciences-deploy` | CodeBuild project | Created/updated by `deploy.sh` | Tagged `cei:owner=deploy.sh`; source: the given GitHub repository URL (normalized to `https://github.com/<owner>/<repo>`, no embedded credentials) and branch; buildspec `buildspec.yml`; image `aws/codebuild/standard:8.0`; each build is started pinned to a resolved commit SHA (`start-build --source-version`), not just the branch; runs `npm ci`, the Region pre-flight, the frontend build, `cdk bootstrap` (where needed), and `cdk deploy --all` for both `BackendStack` and `EdgeWafStack` (B91) |
| `cei/deploy/github-pat` | Secrets Manager secret | Created only if the user supplies a GitHub PAT for a private repository | Tagged `cei:owner=deploy.sh`; attached to the `cei-life-sciences-deploy` project only, through the project's own `source.auth` field (`SECRETS_MANAGER`) — never set as the account-wide default GitHub credential |
| `CDKToolkit` | CloudFormation stack (CDK bootstrap) | Created by the CodeBuild build (not by `deploy.sh` itself), only if not already present | Standard `cdk bootstrap` stack (`--bootstrap-kms-key-id AWS_MANAGED_KEY`), run in `buildspec.yml`'s `build` phase, in the target Region; also bootstrapped in `us-east-1` if the target Region is not `us-east-1` (B88 — `EdgeWafStack` always deploys there); not part of `BackendStack`. `deploy.sh` only does a read-only `cloudformation:DescribeStacks` check beforehand, to decide whether the CodeBuild role needs the `cei-deploy-bootstrap` policy above (B91) |

`deploy.sh` checks the `cei:owner=deploy.sh` tag on any same-named existing resource before updating it, and stops with an error instead of modifying or overwriting a resource it does not own.

---

## What exists outside CDK (not AWS-provisioned)

These are local tools, not deployed AWS resources:

- **Local agent development** (`backend/agents/agentcore/`) — the AgentCore CLI project for `agentcore dev`. It is for local use only; CDK does not ship it.
- **Tool agent** (`backend/agents/tool_agent/`) — the graph and YAML profiles exist, but no runtime deploys them yet (F15).
- **Frontend** (`frontend/`) — the `founder-ai-dashboard` Vite SPA (React 19, Tailwind 4, react-router), the production frontend (B1/B66). It is built and deployed to the CDK-provisioned S3 bucket and served through CloudFront (see the `BackendStack` table above). The archived Next.js test harness and its local proxy routes/CEI UI preview renderer now live in `archive/frontend-next/` and are not deployed. The NCBI Vite dev proxy in `frontend/` is still in place (B21 removes it in F15).

## What is not deployed and not built at all

These parts of the target architecture are **planned, not built**: the tool-agent runtime, the User Metadata and Attachment Metadata tables, S3 Vectors, the product API Key Store routes, SQS, Bedrock Knowledge Base, Bedrock Guardrails in the stack, the Web Search connector, and the deletion cascade. (The one Secrets Manager secret that does exist, `cei/deploy/github-pat`, belongs to the deploy mechanism, not the product — see the table above.) See [Architecture Deep Dive](./architectureDeepDive.md) for the full component table, status per component, and `.agents/specs/architecture.md` for detail.

---

## Planned resources

For the target list of every AWS resource this project will provision, with phase and status, see:

- [Architecture Deep Dive](./architectureDeepDive.md) — human-readable component table
- `.agents/specs/architecture.md` — full component table with build phase per resource
- `.agents/decision-log.md` — the decisions behind each resource choice
