# Deployment Guide

Status: **Development work in progress.** The repository defines a REST API, session Lambdas, DynamoDB stores, AgentCore runtimes and Gateway, and monitoring in addition to the frontend hosting resources. A resource defined in CDK is not evidence that it is deployed or that its user journey works. Confirm the stack's deployed commit and CloudFormation resources before operating it. See [Operations](./operations.md) for monitoring and validation gaps, and `.agents/decision-log.md` for the approved architecture.

---

## Deployment model

- **Single environment.** (Exists) No separate dev/staging/prod stacks. Developers use the local AgentCore CLI for agent development before anything is deployed.
- **Region is configurable.** (Exists) `deploy.sh` writes the Region into `backend/cei.deploy.json`. `backend/bin/backend.ts` reads it and passes it to the stack's `env`. There is no fixed Region.
- **AgentCore resources in CDK.** The stack defines the Code Interpreter, specialist runtimes, Gateway and Policy engine, and Research memory. Additional runtime features remain incomplete. `agentcore.json` stays local-development-only.

---

## `deploy.sh` guided setup (Exists)

`deploy.sh` sits at the repo root. It is now a thin (~50-line) bash wrapper. **All build steps still run in CodeBuild, never locally (B91).** The wrapper finds a Python interpreter (`python3`, then `python`) that is version 3.9 or later, creates (or reuses) a local virtual environment at `.deploy-venv/`, installs `deploy/requirements.txt` with `pip` (only when that file has changed since the last run), then runs `python -m cei_deploy` from the `deploy/` directory, passing through any flags. `deploy/cei_deploy` is the real deploy tool (B111): it shows a Textual terminal UI (TUI), collects configuration, then creates or updates the same CodeBuild deployment pipeline and starts a build. `npm ci`, the Region pre-flight check, the frontend build, `cdk bootstrap`, and `cdk deploy` all still run inside that CodeBuild build (see [Backend deployment](#backend-deployment-build-steps) below). `deploy.sh` works in bash on Linux, macOS, CloudShell, and Git Bash.

### Prerequisites

- Python 3.9 or later, as `python3` or `python` on `PATH`, with `pip`.
- git (optional). Without it, the tool cannot auto-fill the repository URL/branch or pin the build to a commit SHA; type them in by hand on the Repository screen instead.
- bash.
- AWS credentials for the target account, discoverable by boto3 (environment variables, a shared `~/.aws/credentials` profile, SSO, or an instance role). The AWS CLI itself, Node.js, and `jq` are **not** needed locally any more — the deploy tool calls AWS directly through boto3.
- CloudShell already has Python 3.9+, pip, git and bash, so it needs no setup beyond signing in.
- Your AWS credentials need, read-only, while the TUI collects configuration (used to pre-fetch data for the screens):
  - `sts:GetCallerIdentity`
  - `bedrock:ListFoundationModels`, `bedrock:ListInferenceProfiles`, `bedrock:GetInferenceProfile` (to fill the Models screen)
  - `cloudformation:DescribeStacks` (the `CDKToolkit` check in the deploy Region and `us-east-1`, and the leftover-spike-stack check)
  - `secretsmanager:DescribeSecret` (to detect a previously stored GitHub PAT)
- and, once you click Deploy (or run `--yes`), the same write permissions as before:
  - `iam:GetRole`, `iam:CreateRole`, `iam:UpdateAssumeRolePolicy`, `iam:PutRolePolicy`, `iam:DeleteRolePolicy`, `iam:TagRole`, `iam:ListRoleTags`, `iam:PassRole` (on `cei-deploy-codebuild-role`)
  - `secretsmanager:CreateSecret`, `secretsmanager:PutSecretValue`, `secretsmanager:TagResource` (only if you give a new PAT)
  - `codebuild:BatchGetProjects`, `codebuild:CreateProject`, `codebuild:UpdateProject`, `codebuild:StartBuild`, `codebuild:BatchGetBuilds`
  - No local CDK bootstrap permission is needed: the tool only writes the `cei-deploy-bootstrap` inline policy onto the CodeBuild role (covered by `iam:PutRolePolicy` above); the CodeBuild role itself, not your local credentials, runs `cdk bootstrap`.

Run `./deploy.sh -h` to see the two flags below.

### Flags

| Flag | Purpose |
|---|---|
| `--dry-run` | With a saved config and no TTY: validate the saved `backend/cei.deploy.json` and stop. With the TUI: run every screen as normal, then on Review & Deploy, write the config and print what a real run would create, with no AWS writes. |
| `--yes` | Redeploy from the saved `backend/cei.deploy.json`, with no TUI. Fails if there is no saved, valid config yet — run the TUI once first. |
| `-h` | Show help and exit. |

Every flag the old bash script had — `--region`, `--repo-url`, `--branch`, `--default-model`, `--router-model`, `--blocked-models`, `--email-domains`, `--admin-email`, `--rps`, `--max-realtime`, `--max-background`, `--allow-localhost-callback`, `--github-pat-stdin`, `--non-interactive`, `--spike` — is gone. The TUI screens below collect the same information interactively, and `--yes` replays a saved config instead.

### The guided screens

**Keyboard.** Every screen works with the arrow keys, Space and Enter only. Up/Down (and Left/Right outside a text field) move between fields and buttons. Space or Enter presses a button, opens a dropdown, toggles a check box or the Advanced section, and checks or unchecks a model. In the model list, Left/Right switch between the Global and US tabs, and Down on the last row (Up on the first) leaves the list. Enter in a text field moves to the next field. Esc goes back one step; Ctrl+Q quits. Tab and Shift+Tab also work. The bar at the bottom of each screen shows these keys.

With no flags, `./deploy.sh` opens a five-screen TUI:

1. **Start** — lets you pick the Region to deploy into. If `backend/cei.deploy.json` already exists, it shows two buttons instead: "Redeploy saved settings (<Region>)" skips straight to Review & Deploy, and "Change settings" opens the Region picker. Your AWS account shows on the Review & Deploy screen; the Start screen shows it only if it cannot be read (as an error).
2. **Basics** — the admin alert email address, and the allowed sign-up email domains. The domains field defaults to the admin email's own domain and keeps following it until you edit the domains field directly.
3. **Models** — see [Model selection](#model-selection) below.
4. **Repository** — the GitHub repository URL and branch (pre-filled from your local git `origin` remote and current branch, converted from SSH to HTTPS form if needed), a GitHub personal access token field that appears only when the repository looks private or its visibility could not be determined, and a collapsed **Advanced** section: the per-user requests/second limit (default 20), max concurrent real-time runs per user (default 2), max concurrent background runs per user (default 1), the Tool Gateway policy mode (`LOG_ONLY` or `ENFORCE`, default `LOG_ONLY`), and a switch to add `http://localhost:5173` as a Cognito callback/logout URL for local frontend development (default off, B90).
5. **Review & Deploy** — a summary of every value above, a warning (with the exact `aws cloudformation delete-stack` command) if a leftover spike stack is still deployed in the Region, a live scrolling log, and the Deploy button (labeled "Dry run" under `--dry-run`).

Use the Back/Next buttons to move between screens; your answers stay in memory as you go back and forth.

### Model selection

The Models screen lists only the latest multimodal (text + image in, text out), tool-capable models from OpenAI, Anthropic, Qwen, Google, Meta, Moonshot, Z.AI and xAI, shown as Bedrock inference profiles under two tabs, **Global** and **US**. Each profile has its own checkbox, so you can allow a model through its `US` profile and block its `Global` profile (for example, for data residency: a `us.` profile keeps requests in US Regions, and a `global.` profile can route them to any commercial Region). Every profile starts checked. A search bar filters the list by name or provider. "Select all" and "Clear all" change only the profiles listed in the open tab (after the search filter).

Unchecked profiles are written to `blockedModelIds` (B113):

- If no profile of a model is checked, the tool writes the bare model ID (for example `anthropic.claude-sonnet-5-5`). An entry with no prefix blocks the model under every profile prefix and its bare, in-Region ID.
- If at least one profile of a model is checked, the tool writes only the unchecked profile IDs (for example `global.anthropic.claude-sonnet-5-5`). An entry with a prefix blocks only that profile. The bare in-Region ID stays allowed, because an in-Region call is the most data-resident option.

`blockedModelIds` also includes every other tool-capable model the backend could otherwise discover (any provider, not only the 8 above), as bare IDs, so the deployed app only ever offers the providers this screen shows. The D3 safety patterns have no prefix, so they still block their models under every prefix. A model AWS releases later, that was never in this list, stays allowed — it is simply absent from `blockedModelIds`.

The screen pre-selects a default model and a router model: the newest Claude Sonnet and the newest Claude Haiku, preferring the `Global` profile over `US` when both exist. Change either with the "Default model" / "Router model" dropdowns, which only ever offer a currently checked model.

A D3-blocked model (`anthropic.claude-fable-5*`, `anthropic.claude-mythos-*`) is never listed, even though Anthropic is in the provider allowlist — it stays out of the picker and out of `blockedModelIds` alike, the same as the backend's own D3 blocklist already treats it.

A cross-Region inference profile lists one model ARN per AWS Region it can route to, all naming the same underlying foundation model; the tool resolves this to that one model (fixed 2026-11-18, together with the matching backend bug in `model_discovery.py` — see B111). A profile whose ARNs name different models stays unresolved and is left out of the picker.

If Bedrock model discovery fails (for example, a missing `bedrock:ListFoundationModels`/`bedrock:ListInferenceProfiles` permission), the screen falls back to two plain text fields for the default and router model IDs, and `blockedModelIds` is left empty.

### Background pre-fetch

The TUI starts some AWS and git lookups in the background so the screens rarely wait on a cold network call:

- At launch, before you confirm a Region: your AWS caller identity, your local git remote/branch, and whether the GitHub repository looks public or private.
- The moment you confirm a Region on the Start screen (never a guessed Region): Bedrock model discovery, a check for a previously stored GitHub PAT secret, a check for the CDK bootstrap stack (`CDKToolkit`) in that Region and in `us-east-1`, and the leftover-spike-stack check.

### Repository URL

The Repository screen only accepts a plain `https://github.com/<owner>/<repo>` URL, optionally ending in `.git`, with no embedded credentials. If your `origin` git remote is in SSH form (`git@github.com:...` or `ssh://git@github.com/...`), the tool converts it to `https://` automatically before pre-filling the field.

### Private repositories

The GitHub personal access token field appears only when the repository looks private, or its visibility could not be determined over the unauthenticated GitHub API. If the tool finds a previously stored token for this account, a "Use stored token" checkbox appears, pre-checked. The token is held only in memory for the life of the TUI process — it is never written to a temp file, to `cei.deploy.json`, or to shell history. On Deploy, it is stored in Secrets Manager as `cei/deploy/github-pat` and attached to the `cei-life-sciences-deploy` CodeBuild project only, through the project's own `source.auth` field (`SECRETS_MANAGER`). The token is never set as the account-wide default GitHub credential, so it does not affect any other CodeBuild project in the account.

### Local dev callback URL

By default, the Cognito SPA app client only accepts the deployed CloudFront domain as a callback/logout URL. On the Repository screen's Advanced section, turn on "Add `http://localhost:5173` as a Cognito callback/logout URL" to also add `http://localhost:5173/auth/callback` and `http://localhost:5173/` (B90). This is a proposal for local development, not a settled CORS/dev-origin decision — leave it off unless you need to run the SPA against a deployed Cognito pool from `npm run dev`.

### Pinned build commit

The tool pins the build to a specific commit SHA, passed to `codebuild start-build --source-version`, and shows it in the Review & Deploy summary as "Source version". It resolves the SHA the same way the original bash script did:

1. If the local `HEAD` commit matches the current remote head of the target branch (checked with `git ls-remote`), it uses local `HEAD`.
2. Otherwise, it asks the remote directly (`git ls-remote <repo-url> refs/heads/<branch>`) for that branch's current commit SHA.
3. If neither resolves (for example, local `HEAD` is not pushed, and the repository cannot be reached), `deploy.sh` prints a warning and falls back to tracking the branch head instead of a pinned commit.

This means: if you have local, unpushed commits, the build does **not** deploy them — it deploys the last commit actually pushed to the branch (or, if even that cannot be resolved, whatever the branch head is at build time).

### What the deploy tool creates

Clicking Deploy on the Review & Deploy screen (or running `--yes`) does the following, in order:

1. Writes `backend/cei.deploy.json` — the collected configuration (not committed; see `.gitignore`). `webSearchEnabled` is written as a placeholder (`false`) here; CodeBuild's pre-flight overwrites it with the real probed value (B91).
2. A read-only `cloudformation:DescribeStacks` check for `CDKToolkit` in the target Region and in `us-east-1` (deduplicated if they are the same Region). This makes no changes — it only decides whether the CodeBuild role needs the one-time `cei-deploy-bootstrap` policy below. The actual `cdk bootstrap` run happens later, inside CodeBuild (B91).
3. The GitHub PAT secret in Secrets Manager, only if you gave one (or chose to reuse a stored one).
4. The IAM role `cei-deploy-codebuild-role` and inline policy `cei-deploy-codebuild-policy` (created or updated).
5. The one-time inline policy `cei-deploy-bootstrap` on that role — attached only for Regions that step 2 found still need `CDKToolkit` (so the CodeBuild role, not your local credentials, can run `cdk bootstrap`), and removed again once no Region needs it.
6. The CodeBuild project `cei-life-sciences-deploy` (created or updated), source set to your repository URL and branch.
7. A CodeBuild build, pinned to the resolved commit SHA, started right away, using `buildspec.yml`. **Every actual build step — `npm ci`, the Region pre-flight, the frontend build, `cdk bootstrap` (where still needed), `cdk deploy` — runs inside that CodeBuild build, never on your machine (B91).** The Review & Deploy screen's log shows the IAM/CodeBuild setup messages first, then the build's console URL and phase changes as they happen.

There is no separate CDK pipeline stack — the role and project are created directly through boto3, not the AWS CLI (B87, refined by B111). `BackendStack` and `EdgeWafStack` are the only two product stacks (B88).

### Ownership tagging and resource takeover protection

Every resource `deploy.sh` creates (the IAM role, the CodeBuild project, and the GitHub PAT secret) is tagged `cei:owner=deploy.sh`. Before updating any same-named resource, `deploy.sh` checks for this tag. If a resource with that name already exists but lacks the tag, `deploy.sh` stops with an error instead of modifying or overwriting it. This stops the script from silently taking over a resource it did not create.

### IAM trust policy scoping

The `cei-deploy-codebuild-role` trust policy only lets `codebuild.amazonaws.com` assume the role when both `aws:SourceAccount` (the deploying account) and `aws:SourceArn` (the specific `cei-life-sciences-deploy` project ARN) match. The role's own permission to assume the CDK bootstrap roles is scoped to `cdk-hnb659fds-*-<account>-<region>` (that account and Region's bootstrap roles only), not a bare wildcard.

### The one-time `cei-deploy-bootstrap` policy (B91)

`deploy.sh` attaches a separate inline policy, `cei-deploy-bootstrap`, to `cei-deploy-codebuild-role`, but only for a run where a Region still needs `CDKToolkit` (per the read-only check in step 2 above); it removes the policy again once no Region needs it. The policy is scoped to exactly what `cdk bootstrap` creates: CloudFormation on the `CDKToolkit` stack; IAM on `cdk-hnb659fds-*-<account>-<region>` roles (including `PassRole` and `AttachRolePolicy`); S3 on the `cdk-hnb659fds-assets-<account>-<region>` bucket; ECR on the `cdk-hnb659fds-container-assets-<account>-<region>` repository; and SSM on `/cdk-bootstrap/hnb659fds/*`. `buildspec.yml` passes `--bootstrap-kms-key-id AWS_MANAGED_KEY`, so no customer-managed KMS key, and no KMS permission, is ever needed here. This policy lets the CodeBuild role run `cdk bootstrap` itself; it does not give your local credentials any new permission.

### Pre-flight failures

The pre-flight script (`backend/scripts/preflight.ts`) runs only inside CodeBuild's `pre_build` phase (B91) — never locally in `deploy.sh`. It checks, before any `cdk bootstrap` or `cdk deploy`: AgentCore Runtime/Gateway/Policy/Memory availability in the Region (SSM service check plus a doc-checked capability matrix), S3 Vectors, Bedrock Knowledge Bases, and that the default and router Bedrock models are available, authorized, entitled, and (for inference profiles) `ACTIVE` in that Region. Any failure prints a specific error to stderr and fails the CodeBuild build before it changes anything else in AWS. Outside the three AgentCore Web Search Regions (us-east-1, eu-west-1, ap-northeast-1), pre-flight does not fail — it only sets `webSearchEnabled: false` in the config.

### Watching the build

The tool starts the build with `codebuild start-build`, prints the build ID and a console log URL, then polls the build status every 15 seconds and prints each phase change — in the Review & Deploy screen's log, or on the terminal under `--yes`. The run exits 0 on `SUCCEEDED`, or exits 1 and prints the console URL on any other end status. You can also open the console URL at any time to watch the live log.

### `--dry-run`

With a saved config and no TTY, `--dry-run` only validates `backend/cei.deploy.json` and stops. With a TTY, it still opens the full TUI; every screen behaves the same, but the Review & Deploy screen's button reads "Dry run" instead of "Deploy". Clicking it writes the config, then stops before any AWS write (including the read-only `CDKToolkit` check), and prints a short list of what a real run would create (the IAM role, the CodeBuild project, and GitHub source credentials, if a PAT was given). The Region pre-flight itself only runs later, inside CodeBuild — a dry run does not exercise it.

### Leftover spike stacks

All isolated spike code (`backend/lib/spikes/`, the spike Lambdas, the spike agent entry points, and their tests) was removed on 2026-11-18 once the spikes it supported had served their purpose (B92, closed by B111). If a spike stack (`ToolScopeSpikeStack`, `StreamingSpikeStack`, `DurableSpikeStack`, or `IdentitySpikeStack`) is still deployed in the target Region from an earlier spike run, the Review & Deploy screen's summary lists it and prints the exact `aws cloudformation delete-stack` command to remove it. The tool only warns — it never deletes a stack for you.

---

## Backend deployment (build steps)

`buildspec.yml` (Exists) runs in CodeBuild. **This is where every build step actually happens (B91) — `deploy.sh` runs none of these steps itself:**

1. `install` — `npm ci` in `backend/` (Node.js 22 runtime) **and** in `frontend/`.
2. `pre_build` — decode the configuration and write it to `backend/cei.deploy.json`; run the Region pre-flight (`npx ts-node scripts/preflight.ts --config cei.deploy.json --write`). This is the only place the pre-flight runs; a failure here fails the build before any `cdk bootstrap` or `cdk deploy`.
3. `build` — `npm run build` in `frontend/` first (produces `frontend/dist`); then, for each Region that still needs `CDKToolkit` (the deploy Region and `us-east-1`, deduplicated — `EdgeWafStack` always deploys to `us-east-1`), `npx cdk bootstrap --bootstrap-kms-key-id AWS_MANAGED_KEY`; then `npm run build` and `npm test` in `backend/`; then `cdk deploy --all --require-approval never -c ceiConfigFile=cei.deploy.json`.

The frontend build must run before `cdk bootstrap` and `cdk deploy`: `backend/bin/backend.ts` resolves `frontendAssetPath` to `../frontend/dist` and stops with a clear error ("build the frontend first: cd frontend && npm ci && npm run build") if that directory does not exist, because `BackendStack`'s `BucketDeployment` needs a real asset directory at synth time, and `cdk bootstrap` also triggers a synth. (This is exactly the failure the user hit running `deploy.sh` from AWS CloudShell before B91: the old flow ran `cdk bootstrap` locally, before `frontend/dist` existed.)

The CodeBuild deployment targets `BackendStack` and `EdgeWafStack` (the latter is always in `us-east-1`). Product capability and deployed status must be checked separately; a successful stack operation does not prove specialist workflows, identity transport, deletion, or async recovery. File uploads and managed RAG are deferred in the current iteration.

---

## Frontend deployment (Exists, F2)

The production frontend is the `founder-ai-dashboard` Vite SPA in `frontend/` (replacing the archived Next.js test harness, now in `archive/frontend-next/` — see [Architecture Deep Dive](./architectureDeepDive.md), B1/B66), served from **S3 + CloudFront + WAF**. There is no Vercel or Amplify deployment target for production; those options are removed from this project's plan.

Steps performed by `buildspec.yml` in CodeBuild:

1. Build the frontend into `frontend/dist`.
2. Deploy the CDK stacks from the build environment. `SiteAssetsDeployment` uploads `frontend/dist` to the private site bucket, then `RuntimeConfigDeployment` writes `/config.json` (region, Cognito user pool ID, SPA client ID, Cognito domain) with `Cache-Control: no-cache`. Each deployment invalidates the CloudFront distribution for the paths it touched.
3. The SPA fetches `/config.json` before it renders (see `frontend/src/main.tsx`), so the same static build works against any deployment.

CloudFront serves the SPA fallback for client-side routing: a 403 or 404 from S3 (for example on a deep link like `/history`) returns `/index.html` with HTTP 200 instead of an error page.

---

## Post-deployment verification

After `./deploy.sh` succeeds, verify (see `.agents/phases/F02-hosting-auth.md` task 10; the user ran this check on a real deployment and it passed on 2026-10-05):

1. Open a deep link (for example `<SiteUrl>/history`) — the SPA loads (the CloudFront fallback works).
2. Sign up with a disallowed email domain — Cognito rejects it (the pre-sign-up Lambda trigger).
3. Sign up with an allowed email domain — receive the verification email, verify, log in, and land on the dashboard shell with no report pages in navigation.
4. Reload the page — you stay logged in (a silent redirect through the Cognito session cookie); click "Sign out" — you return to Cognito managed login.
5. Test the WAF rule only in an isolated development environment using the bounded procedure agreed for that environment; check both the response and the relevant WAF metric.

Also test `/chat/stream` end to end and confirm the configured SNS email subscription and alarms work in the development account. Follow the bounded checks and limitations in [Operations](./operations.md); do not use live user data for testing.

---

## Cleanup

Stack deletion is destructive and is not part of a normal deployment. Before deleting, inspect both deployed CloudFormation templates and each resource's deletion and update-replace policies; some data stores are retained. Follow the approved environment cleanup procedure and verify retained resources afterwards.

**The Cognito user pool is retained.** `deletionProtection: true` and `RemovalPolicy.RETAIN` on `UserPool` mean `cdk destroy` does not delete it — deletion protection blocks CloudFormation from removing it even if you try, and the removal policy also tells CloudFormation to leave it in place on stack deletion. To remove it by hand once you are sure the user data is no longer needed: in the Cognito console (or `aws cognito-idp`), first turn off deletion protection on the user pool (`UpdateUserPool` with `DeletionProtection: INACTIVE`), then delete the user pool. This permanently deletes every user account in it.

`deploy.sh` (through the `deploy/cei_deploy` tool) creates some resources directly through boto3, outside CDK. `cdk destroy` does **not** remove these. To remove them by hand: delete the CodeBuild project `cei-life-sciences-deploy`, delete the IAM role `cei-deploy-codebuild-role`, and (if you gave a GitHub PAT) delete the Secrets Manager secret `cei/deploy/github-pat`. If a spike stack is still deployed (see [Leftover spike stacks](#leftover-spike-stacks) above), delete it too: `aws cloudformation delete-stack --stack-name <name>`.

---

## Next steps

- [Architecture Deep Dive](./architectureDeepDive.md) — target architecture and current status per component
- [Provisioned Resources](./provisioned-resources.md) — historical resource inventory; compare with the actual deployed stack
- `.agents/specs/architecture.md` — full target architecture spec
- `.agents/decision-log.md` — decisions behind the deployment design (B45–B48, B87–B92, B111)
- `.agents/phases/F01-deploy-foundation.md` — the F1 build phase that delivered `deploy.sh`
- `.agents/phases/F02-hosting-auth.md` — the F2 build phase that delivered Cognito, the S3/CloudFront/WAF-fronted dashboard, and the edge WAF stack
