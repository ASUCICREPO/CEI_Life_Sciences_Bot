# Deployment Guide

Status: **Partly built.** `deploy.sh` exists and works today (F1, B87). It sets up a CodeBuild project that runs `cdk deploy`. But the CDK stack it deploys (`BackendStack`) still defines only the AgentCore Code Interpreter — the full product stack (Cognito, API Gateway, Lambdas, tables, and so on) is **planned, not built**. This guide marks each part "Exists" or "Planned". Full detail: `.agents/specs/architecture.md`, `.agents/decision-log.md` (B45–B48, B87), `.agents/phases/F01-deploy-foundation.md`.

---

## Deployment model

- **Single environment.** (Exists) No separate dev/staging/prod stacks. Developers use the local AgentCore CLI for agent development before anything is deployed.
- **Region is configurable.** (Exists) `deploy.sh` writes the Region into `backend/cei.deploy.json`. `backend/bin/backend.ts` reads it and passes it to the stack's `env`. There is no fixed Region.
- **All AgentCore resources go into CDK.** (Planned) Today only the Code Interpreter is in CDK. Runtimes, Gateway and targets, Memory, and the Policy engine are not yet defined. `agentcore.json` stays local-development-only.

---

## `deploy.sh` guided setup (Exists)

`deploy.sh` sits at the repo root. It collects configuration, runs a Region pre-flight check, then creates or updates a CodeBuild project and starts a build. It works in bash on Linux, macOS, CloudShell, and Git Bash.

### Prerequisites

- AWS CLI v2, signed in with credentials for the target account.
- Node.js >= 20 and npm.
- Your AWS credentials need at least:
  - `iam:GetRole`, `iam:CreateRole`, `iam:UpdateAssumeRolePolicy`, `iam:PutRolePolicy`, `iam:TagRole`, `iam:PassRole` (on `cei-deploy-codebuild-role`)
  - `secretsmanager:CreateSecret`, `secretsmanager:PutSecretValue`, `secretsmanager:DescribeSecret`, `secretsmanager:TagResource` (only if a PAT is given)
  - `codebuild:BatchGetProjects`, `codebuild:CreateProject`, `codebuild:UpdateProject`, `codebuild:StartBuild`, `codebuild:BatchGetBuilds`
  - `cloudformation:DescribeStacks`
  - `ssm:GetParameter`
  - `bedrock:GetFoundationModelAvailability`, `bedrock:GetInferenceProfile`
  - Plus the (broader, one-time) CDK bootstrap permissions if `CDKToolkit` is not already bootstrapped in the Region

Run `./deploy.sh --help` to see this list from the script itself.

### Flags

| Flag | Purpose |
|---|---|
| `--region <region>` | AWS Region to deploy into |
| `--repo-url <url>` | GitHub repository URL (default: the `origin` git remote) |
| `--branch <branch>` | Git branch to deploy (default: the current branch) |
| `--default-model <model-id>` | Default Bedrock model ID |
| `--router-model <model-id>` | Router classifier Bedrock model ID |
| `--blocked-models <list>` | Comma-separated blocked Bedrock model IDs |
| `--email-domains <list>` | Comma-separated allowed sign-up email domains |
| `--admin-email <email>` | Admin alert email address |
| `--rps <n>` | Per-user requests/second limit (default 20) |
| `--max-realtime <n>` | Max concurrent real-time runs per user (default 2) |
| `--max-background <n>` | Max concurrent background runs per user (default 1) |
| `--github-pat-stdin` | Read a GitHub PAT from stdin (for private repos, non-interactive use) |
| `--non-interactive` | Never prompt; fail if a required value is missing |
| `--yes` | Skip the confirmation prompt |
| `--dry-run` | Run pre-flight only; make no AWS writes |
| `-h`, `--help` | Show help and exit |

If you omit a flag, `deploy.sh` prompts for it (unless `--non-interactive` is set). Interactive prompts always read from `/dev/tty`, so `--github-pat-stdin` can pipe a token over stdin in the same invocation without a prompt consuming it. If given, `--github-pat-stdin` reads its input first, before any other prompt runs.

### Repository URL

`deploy.sh` only accepts a plain `https://github.com/<owner>/<repo>` URL, optionally ending in `.git`, with no embedded credentials. If your `origin` git remote is in SSH form (`git@github.com:...` or `ssh://git@github.com/...`), `deploy.sh` converts it to `https://` automatically before using it as the default. A rejected URL is never echoed back with any embedded credentials still visible (they are redacted first).

### Private repositories

If your repository is private, answer "yes" when `deploy.sh` asks, then paste a GitHub personal access token (input is hidden, read from `/dev/tty`). `deploy.sh` never writes the token to disk, to `cei.deploy.json`, or to shell history. It stores the token in Secrets Manager as `cei/deploy/github-pat`, and attaches it to the `cei-life-sciences-deploy` CodeBuild project only, through the project's own `source.auth` field (`SECRETS_MANAGER`). The token is never set as the account-wide default GitHub credential, so it does not affect any other CodeBuild project in the account.

### Pinned build commit

`deploy.sh` pins the build to a specific commit SHA, passed to `codebuild start-build --source-version`, and prints it in the configuration summary as "Source version". It resolves the SHA as follows:

1. If the local `HEAD` commit matches the current remote head of the target branch (checked with `git ls-remote`), it uses local `HEAD`.
2. Otherwise, it asks the remote directly (`git ls-remote <repo-url> refs/heads/<branch>`) for that branch's current commit SHA.
3. If neither resolves (for example, local `HEAD` is not pushed, and the repository cannot be reached), `deploy.sh` prints a warning and falls back to tracking the branch head instead of a pinned commit.

This means: if you have local, unpushed commits, the build does **not** deploy them — it deploys the last commit actually pushed to the branch (or, if even that cannot be resolved, whatever the branch head is at build time).

### What `deploy.sh` creates

1. `backend/cei.deploy.json` — the collected configuration (not committed; see `.gitignore`).
2. Runs `npm ci` in `backend/`, then the Region pre-flight check (see below). If pre-flight fails, the script stops before touching AWS.
3. `cdk bootstrap`, only if the `CDKToolkit` stack is not already in the target Region.
4. The GitHub PAT secret in Secrets Manager, only if you gave one.
5. The IAM role `cei-deploy-codebuild-role` and inline policy `cei-deploy-codebuild-policy` (created or updated).
6. The CodeBuild project `cei-life-sciences-deploy` (created or updated), source set to your repository URL and branch.
7. A CodeBuild build, pinned to the resolved commit SHA, started right away, using `buildspec.yml`.

There is no separate CDK pipeline stack — the role and project are created directly with the AWS CLI (B87). `BackendStack` stays the only product stack.

### Ownership tagging and resource takeover protection

Every resource `deploy.sh` creates (the IAM role, the CodeBuild project, and the GitHub PAT secret) is tagged `cei:owner=deploy.sh`. Before updating any same-named resource, `deploy.sh` checks for this tag. If a resource with that name already exists but lacks the tag, `deploy.sh` stops with an error instead of modifying or overwriting it. This stops the script from silently taking over a resource it did not create.

### IAM trust policy scoping

The `cei-deploy-codebuild-role` trust policy only lets `codebuild.amazonaws.com` assume the role when both `aws:SourceAccount` (the deploying account) and `aws:SourceArn` (the specific `cei-life-sciences-deploy` project ARN) match. The role's own permission to assume the CDK bootstrap roles is scoped to `cdk-hnb659fds-*-<account>-<region>` (that account and Region's bootstrap roles only), not a bare wildcard.

### Pre-flight failures

The pre-flight script (`backend/scripts/preflight.ts`) checks, before any CodeBuild or CDK action: AgentCore Runtime/Gateway/Policy/Memory availability in the Region (SSM service check plus a doc-checked capability matrix), S3 Vectors, Bedrock Knowledge Bases, and that the default and router Bedrock models are available, authorized, entitled, and (for inference profiles) `ACTIVE` in that Region. Any failure prints a specific error to stderr and stops `deploy.sh` before it creates or changes anything in AWS. Outside the three AgentCore Web Search Regions (us-east-1, eu-west-1, ap-northeast-1), pre-flight does not fail — it only sets `webSearchEnabled: false` in the config.

### Watching the build

`deploy.sh` starts the build with `codebuild start-build`, prints the build ID and a console log URL, then polls the build status every 15 seconds and prints each phase change. It exits 0 on `SUCCEEDED`, or exits 1 and prints the console URL on any other end status. You can also open the console URL at any time to watch the live log.

### `--dry-run`

`--dry-run` runs every step through pre-flight, then stops. It prints what a real run would create, but makes no AWS writes.

---

## Backend deployment (build steps)

`buildspec.yml` (Exists) runs in CodeBuild:

1. `install` — `npm ci` in `backend/` (Node.js 22 runtime).
2. `pre_build` — decode the configuration and write it to `backend/cei.deploy.json`; run the Region pre-flight again (catches Region drift between the local run and the build).
3. `build` — `npm run build`, `npm test`, then `cdk deploy --all --require-approval never -c ceiConfigFile=cei.deploy.json`.

Today, `cdk deploy` only deploys the Code Interpreter construct and the `CodeInterpreterId`/`WebSearchEnabled` stack outputs (Planned: Cognito, the REST API Gateway, Session Runner/Router Lambdas, AgentCore Gateway and Policy, AgentCore Runtimes, DynamoDB tables, S3 buckets, SQS, Secrets Manager, and CloudWatch alarms/SNS — these are added by later phases, F2 onward). See [Provisioned Resources](./provisioned-resources.md) for the exact current list.

---

## Planned: frontend deployment

The production frontend target is a static build of the Vite dashboard (replacing the current Next.js `frontend/` test harness — see [Architecture Deep Dive](./architectureDeepDive.md), B1/B66), served from **S3 + CloudFront**. There is no Vercel or Amplify deployment target for production; those options are removed from this project's plan.

Planned steps: build the static SPA, upload it to the S3 bucket created by CDK, and invalidate the CloudFront distribution. CloudFront needs an SPA fallback (403/404 → `index.html`) for client-side routing.

---

## Planned: post-deployment verification

Once the above exists, verification will include: checking the CloudFormation stack status, confirming the Cognito pool accepts sign-ups from allowed email domains, testing `/chat/stream` end to end, and confirming CloudWatch alarms reach the admin email.

---

## Cleanup

`cdk destroy` only removes what CDK manages — today, the Code Interpreter construct and its CloudFormation output:

```bash
cd backend
cdk destroy
```

> **Warning:** once the full stack exists, `cdk destroy` will remove all deployed product resources — confirm nothing important is only stored there before running it.

`deploy.sh` creates some resources with the AWS CLI, outside CDK. `cdk destroy` does **not** remove these. To remove them by hand: delete the CodeBuild project `cei-life-sciences-deploy`, delete the IAM role `cei-deploy-codebuild-role`, and (if you gave a GitHub PAT) delete the Secrets Manager secret `cei/deploy/github-pat`.

---

## Next steps

- [Architecture Deep Dive](./architectureDeepDive.md) — target architecture and current status per component
- [Provisioned Resources](./provisioned-resources.md) — what is really deployed today
- `.agents/specs/architecture.md` — full target architecture spec
- `.agents/decision-log.md` — decisions behind the deployment design (B45–B48, B87)
- `.agents/phases/F01-deploy-foundation.md` — the F1 build phase that delivered `deploy.sh`
