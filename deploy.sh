#!/usr/bin/env bash
# CEI Life Sciences Bot — guided deployment (F1, B47).
#
# Collects deployment configuration, runs the backend pre-flight Region check,
# then creates/updates a CodeBuild project that builds and `cdk deploy`s the
# BackendStack. Works in bash on Linux, macOS, CloudShell and Git Bash.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"
CONFIG_FILE="$BACKEND_DIR/cei.deploy.json"

ROLE_NAME="cei-deploy-codebuild-role"
ROLE_POLICY_NAME="cei-deploy-codebuild-policy"
PROJECT_NAME="cei-life-sciences-deploy"
GITHUB_PAT_SECRET_NAME="cei/deploy/github-pat"
# ADR: standard:8.0 (Ubuntu 24.04) is the current CodeBuild-recommended image | Rationale: supports nodejs 22/24/26 runtime-versions; re-verify against AWS docs if CodeBuild's recommended image has moved on
BUILD_IMAGE="aws/codebuild/standard:8.0"

# ---------------------------------------------------------------------------
# Temp file bookkeeping — nothing sensitive is ever written outside these,
# and this trap deletes every one of them on exit (success, failure or Ctrl-C).
# ---------------------------------------------------------------------------
TMP_FILES=()
cleanup() {
  local f
  for f in "${TMP_FILES[@]:-}"; do
    if [ -n "${f:-}" ] && [ -f "$f" ]; then
      rm -f "$f"
    fi
  done
  return 0
}
# ADR: trap EXIT, INT and TERM | Rationale: a Ctrl-C or kill mid-run must still delete the PAT/policy temp files
trap cleanup EXIT
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM

# mktemp_secure <var_name> — creates a 0600 temp file, tracks it for cleanup, and
# assigns its path to <var_name> by reference. Must NOT be called as
# `x="$(mktemp_secure)"` — that runs in a subshell, so the TMP_FILES append is
# lost and the file is never cleaned up.
mktemp_secure() {
  local f
  f="$(umask 077; mktemp)"
  TMP_FILES+=("$f")
  printf -v "$1" '%s' "$f"
}

print_help() {
  cat <<'EOF'
Usage: ./deploy.sh [options]

Collects CEI Life Sciences Bot deployment configuration, runs the backend
Region pre-flight check, then creates/updates the CodeBuild deployment
pipeline and starts a build.

Options:
  --region <region>            AWS Region to deploy into
  --repo-url <url>              GitHub repository URL (default: git remote "origin")
  --branch <branch>             Git branch to deploy (default: current branch)
  --default-model <model-id>    Default Bedrock model ID
  --router-model <model-id>     Router classifier Bedrock model ID
  --blocked-models <list>       Comma-separated blocked Bedrock model IDs
  --email-domains <list>        Comma-separated allowed sign-up email domains
  --admin-email <email>         Admin alert email address
  --rps <n>                     Per-user requests/second limit (default: 20)
  --max-realtime <n>            Max concurrent real-time runs/user (default: 2)
  --max-background <n>          Max concurrent background runs/user (default: 1)
  --allow-localhost-callback     Add http://localhost:5173 as an extra Cognito
                                 SPA callback/logout URL (B90, local dev only)
  --github-pat-stdin            Read a GitHub personal access token from stdin
                                 (for private repos, non-interactive use)
  --non-interactive              Never prompt; fail if a required value is missing
  --yes                         Skip the confirmation prompt
  --dry-run                     Run pre-flight only; make no AWS writes
  -h, --help                    Show this help and exit

Interactive mode (no --non-interactive) prompts for any value not supplied by
a flag, and for a GitHub PAT if you say the repository is private. Interactive
prompts always read from /dev/tty, so `--github-pat-stdin` can safely pipe the
token over stdin in the same invocation without a prompt consuming it. Values
never appear in cei.deploy.json, in shell history, or in any process's
command-line arguments.

The repository URL must be a plain https://github.com/<org>/<repo>(.git) URL
(no embedded credentials); an "origin" remote in SSH form is converted
automatically. The started build is pinned to a commit SHA: the local HEAD if
it matches the remote branch head, else the branch's current remote SHA
(resolved via `git ls-remote`); if neither can be resolved, the branch head is
used instead and a warning is printed.

Your AWS credentials need at least:
  iam: GetRole, CreateRole, UpdateAssumeRolePolicy, PutRolePolicy, TagRole,
       PassRole (on cei-deploy-codebuild-role)
  secretsmanager: CreateSecret, PutSecretValue, DescribeSecret, TagResource
       (only if a PAT is given)
  codebuild: BatchGetProjects, CreateProject, UpdateProject, StartBuild,
       BatchGetBuilds
  cloudformation: DescribeStacks
  ssm: GetParameter
  bedrock: GetFoundationModelAvailability, GetInferenceProfile
  Plus the (broader, one-time) CDK bootstrap permissions if CDKToolkit is not
  already bootstrapped in the Region.
EOF
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
REGION_FLAG=""
REPO_URL_FLAG=""
BRANCH_FLAG=""
DEFAULT_MODEL_FLAG=""
ROUTER_MODEL_FLAG=""
BLOCKED_MODELS_FLAG=""
EMAIL_DOMAINS_FLAG=""
ADMIN_EMAIL_FLAG=""
RPS_FLAG=""
MAX_REALTIME_FLAG=""
MAX_BACKGROUND_FLAG=""
ALLOW_LOCALHOST_CALLBACK_FLAG=""
GITHUB_PAT_STDIN="false"
NON_INTERACTIVE="false"
ASSUME_YES="false"
DRY_RUN="false"

while [ $# -gt 0 ]; do
  case "$1" in
    --region) REGION_FLAG="${2:-}"; shift 2 ;;
    --repo-url) REPO_URL_FLAG="${2:-}"; shift 2 ;;
    --branch) BRANCH_FLAG="${2:-}"; shift 2 ;;
    --default-model) DEFAULT_MODEL_FLAG="${2:-}"; shift 2 ;;
    --router-model) ROUTER_MODEL_FLAG="${2:-}"; shift 2 ;;
    --blocked-models) BLOCKED_MODELS_FLAG="${2:-}"; shift 2 ;;
    --email-domains) EMAIL_DOMAINS_FLAG="${2:-}"; shift 2 ;;
    --admin-email) ADMIN_EMAIL_FLAG="${2:-}"; shift 2 ;;
    --rps) RPS_FLAG="${2:-}"; shift 2 ;;
    --max-realtime) MAX_REALTIME_FLAG="${2:-}"; shift 2 ;;
    --max-background) MAX_BACKGROUND_FLAG="${2:-}"; shift 2 ;;
    --allow-localhost-callback) ALLOW_LOCALHOST_CALLBACK_FLAG="true"; shift 1 ;;
    --github-pat-stdin) GITHUB_PAT_STDIN="true"; shift 1 ;;
    --non-interactive) NON_INTERACTIVE="true"; shift 1 ;;
    --yes) ASSUME_YES="true"; shift 1 ;;
    --dry-run) DRY_RUN="true"; shift 1 ;;
    -h|--help) print_help; exit 0 ;;
    *) echo "Unknown option: $1" >&2; print_help; exit 1 ;;
  esac
done

# ADR: read a piped GitHub PAT immediately, before any interactive prompt | Rationale:
# every later prompt reads from /dev/tty (not stdin), but this read happens first anyway
# so a piped token can never be consumed by mistake even on platforms where /dev/tty is unusable
GITHUB_PAT=""
if [ "$GITHUB_PAT_STDIN" = "true" ]; then
  if [ -t 0 ]; then
    # ADR: -s (silent) when stdin is a real terminal | Rationale: security review — `--github-pat-stdin`
    # is meant for piped, non-interactive use; if stdin is actually a TTY, a plain `read` would echo the
    # token to the screen as it's typed. `-s` suppresses its own newline echo too, so print one after.
    IFS= read -rs GITHUB_PAT || [ -n "$GITHUB_PAT" ]
    echo
  else
    IFS= read -r GITHUB_PAT || [ -n "$GITHUB_PAT" ]
  fi
fi

# ---------------------------------------------------------------------------
# Step 1 — prerequisite tools and AWS identity
# ---------------------------------------------------------------------------
require_cmd() {
  command -v "$1" >/dev/null 2>&1 || { echo "Required command not found: $1" >&2; exit 1; }
}

require_cmd aws
require_cmd node
require_cmd npm

NODE_VERSION="$(node -v | sed 's/^v//')"
NODE_MAJOR="${NODE_VERSION%%.*}"
if [ "$NODE_MAJOR" -lt 20 ]; then
  echo "Node.js >= 20 is required (found v$NODE_VERSION)." >&2
  exit 1
fi

if ! ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text 2>/dev/null)"; then
  echo "Could not call 'aws sts get-caller-identity'. Configure AWS credentials first." >&2
  exit 1
fi
CALLER_ARN="$(aws sts get-caller-identity --query Arn --output text)"
PARTITION="$(printf '%s' "$CALLER_ARN" | cut -d: -f2)"

# ---------------------------------------------------------------------------
# Step 2 — collect configuration (flags, then prompts, with defaults)
# ---------------------------------------------------------------------------
prompt() {
  # prompt <var_name> <flag_value> <prompt_text> <default_value> <required>
  local var_name="$1" current="$2" prompt_text="$3" default_value="$4" required="$5"
  if [ -z "$current" ] && [ "$NON_INTERACTIVE" = "true" ]; then
    current="$default_value"
  fi
  if [ -z "$current" ] && [ "$NON_INTERACTIVE" != "true" ]; then
    # ADR: prompts read from /dev/tty, not stdin | Rationale: --github-pat-stdin pipes the
    # token over stdin; prompts must never compete with it for the same input stream
    if [ -n "$default_value" ]; then
      read -rp "$prompt_text [$default_value]: " current < /dev/tty
      current="${current:-$default_value}"
    else
      read -rp "$prompt_text: " current < /dev/tty
    fi
  fi
  if [ -z "$current" ] && [ "$required" = "true" ]; then
    echo "Missing required value: $prompt_text" >&2
    exit 1
  fi
  printf -v "$var_name" '%s' "$current"
}

# ADR: normalize an SSH-form GitHub remote to https:// before it is ever shown or validated |
# Rationale: the validation regex below only accepts a plain https://github.com/<org>/<repo> URL
to_https_github_url() {
  local u="$1"
  case "$u" in
    git@github.com:*) printf 'https://github.com/%s' "${u#git@github.com:}" ;;
    ssh://git@github.com/*) printf 'https://github.com/%s' "${u#ssh://git@github.com/}" ;;
    *) printf '%s' "$u" ;;
  esac
}

DEFAULT_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
DEFAULT_REPO_URL="$(to_https_github_url "$(git -C "$SCRIPT_DIR" remote get-url origin 2>/dev/null || true)")"
DEFAULT_BRANCH="$(git -C "$SCRIPT_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)"

prompt REGION "$REGION_FLAG" "AWS Region to deploy into" "$DEFAULT_REGION" true
prompt REPO_URL "$REPO_URL_FLAG" "GitHub repository URL" "$DEFAULT_REPO_URL" true
REPO_URL="$(to_https_github_url "$REPO_URL")"
prompt BRANCH "$BRANCH_FLAG" "Git branch to deploy" "$DEFAULT_BRANCH" true
prompt DEFAULT_MODEL "$DEFAULT_MODEL_FLAG" "Default Bedrock model ID" "" true
prompt ROUTER_MODEL "$ROUTER_MODEL_FLAG" "Router classifier Bedrock model ID" "" true
prompt BLOCKED_MODELS "$BLOCKED_MODELS_FLAG" "Blocked Bedrock model IDs (comma-separated, blank for none)" "" false
prompt EMAIL_DOMAINS "$EMAIL_DOMAINS_FLAG" "Allowed sign-up email domains (comma-separated)" "" true
prompt ADMIN_EMAIL "$ADMIN_EMAIL_FLAG" "Admin alert email address" "" true
prompt RPS "$RPS_FLAG" "Per-user requests/second limit" "20" true
prompt MAX_REALTIME "$MAX_REALTIME_FLAG" "Max concurrent real-time runs per user" "2" true
prompt MAX_BACKGROUND "$MAX_BACKGROUND_FLAG" "Max concurrent background runs per user" "1" true

# ADR: a boolean switch, not a prompt-with-default value | Rationale: B90 — this is a local-dev-only
# proposal (security.md CORS/dev-origin TBD), so it must default to false and only turn on when asked
if [ "$ALLOW_LOCALHOST_CALLBACK_FLAG" = "true" ]; then
  ALLOW_LOCALHOST_CALLBACK="true"
elif [ "$NON_INTERACTIVE" = "true" ]; then
  ALLOW_LOCALHOST_CALLBACK="false"
else
  ALLOW_LOCALHOST_CALLBACK_ANSWER=""
  read -rp "Add http://localhost:5173 as a Cognito callback/logout URL for local dev? [y/N]: " ALLOW_LOCALHOST_CALLBACK_ANSWER < /dev/tty
  case "$ALLOW_LOCALHOST_CALLBACK_ANSWER" in
    [Yy]|[Yy][Ee][Ss]) ALLOW_LOCALHOST_CALLBACK="true" ;;
    *) ALLOW_LOCALHOST_CALLBACK="false" ;;
  esac
fi

# GITHUB_PAT was already read from stdin above (--github-pat-stdin) if that flag was set.
if [ "$GITHUB_PAT_STDIN" != "true" ] && [ "$NON_INTERACTIVE" != "true" ]; then
  IS_PRIVATE=""
  read -rp "Is this a private repository? [y/N]: " IS_PRIVATE < /dev/tty
  case "$IS_PRIVATE" in
    [Yy]|[Yy][Ee][Ss])
      read -rsp "GitHub personal access token (input hidden): " GITHUB_PAT < /dev/tty
      echo
      ;;
  esac
fi

# ---------------------------------------------------------------------------
# Simple format validation (deep model/limit validation is the TS pre-flight's job)
# ---------------------------------------------------------------------------
fail() { echo "$1" >&2; exit 1; }

# ADR: redact before printing on validation failure | Rationale: a rejected repo URL may carry
# embedded credentials (https://user:token@host/...); never echo those back to the terminal/logs
redact_url() {
  printf '%s' "$1" | sed -E 's#(https?://)[^@[:space:]/]*@#\1REDACTED@#'
}

[[ "$REGION" =~ ^[a-z]{2}(-gov)?-[a-z]+-[0-9]+$ ]] || fail "Region '$REGION' does not look like a valid AWS Region."
# ADR: only a plain https://github.com/<org>/<repo>(.git) URL is accepted | Rationale: rules out
# embedded credentials (user:token@host); an SSH-form "origin" default is converted above instead
[[ "$REPO_URL" =~ ^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(\.git)?$ ]] || fail "Repository URL does not look like a plain https://github.com/<org>/<repo>(.git) URL (redacted): $(redact_url "$REPO_URL")"
[ -n "$BRANCH" ] || fail "Branch must not be empty."
[[ "$ADMIN_EMAIL" =~ ^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$ ]] || fail "Admin email '$ADMIN_EMAIL' is not a valid email address."
[[ "$RPS" =~ ^[0-9]+$ ]] && [ "$RPS" -gt 0 ] || fail "--rps must be a positive integer."
[[ "$MAX_REALTIME" =~ ^[0-9]+$ ]] && [ "$MAX_REALTIME" -gt 0 ] || fail "--max-realtime must be a positive integer."
[[ "$MAX_BACKGROUND" =~ ^[0-9]+$ ]] && [ "$MAX_BACKGROUND" -gt 0 ] || fail "--max-background must be a positive integer."

IFS=',' read -ra DOMAIN_ARR <<< "$EMAIL_DOMAINS"
for d in "${DOMAIN_ARR[@]}"; do
  d="$(printf '%s' "$d" | xargs)"
  [ -z "$d" ] && continue
  [[ "$d" =~ ^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]] || fail "Email domain '$d' does not look like a valid domain."
done

# ---------------------------------------------------------------------------
# Resolve a commit SHA to pin the build to (falls back to the branch head).
# ---------------------------------------------------------------------------
resolve_source_version() {
  local sha="" local_head="" remote_head="" remote_sha=""
  local_head="$(git -C "$SCRIPT_DIR" rev-parse HEAD 2>/dev/null || true)"
  if [ -n "$local_head" ]; then
    remote_head="$(git -C "$SCRIPT_DIR" ls-remote origin "refs/heads/$BRANCH" 2>/dev/null | cut -f1 || true)"
    if [ "$remote_head" = "$local_head" ]; then
      sha="$local_head"
    fi
  fi
  if [ -z "$sha" ]; then
    remote_sha="$(GIT_TERMINAL_PROMPT=0 git ls-remote "$REPO_URL" "refs/heads/$BRANCH" 2>/dev/null | cut -f1 || true)"
    [ -n "$remote_sha" ] && sha="$remote_sha"
  fi
  printf '%s' "$sha"
}

SOURCE_VERSION="$(resolve_source_version)"
if [ -z "$SOURCE_VERSION" ]; then
  echo "Warning: could not resolve a commit SHA for '$BRANCH' (local HEAD does not match the remote branch, and 'git ls-remote' could not reach the repository). The build will track the branch head instead of a pinned commit." >&2
  SOURCE_VERSION="$BRANCH"
fi

# ---------------------------------------------------------------------------
# Summary and confirmation
# ---------------------------------------------------------------------------
cat <<SUMMARY

Deployment configuration
  Region:                 $REGION
  Repository:              $REPO_URL @ $BRANCH
  Source version:          $SOURCE_VERSION
  Default model:           $DEFAULT_MODEL
  Router model:            $ROUTER_MODEL
  Blocked models:          ${BLOCKED_MODELS:-(none)}
  Allowed email domains:   $EMAIL_DOMAINS
  Admin email:             $ADMIN_EMAIL
  Requests/sec limit:      $RPS
  Max real-time runs/user: $MAX_REALTIME
  Max background runs:     $MAX_BACKGROUND
  Allow localhost callback: $ALLOW_LOCALHOST_CALLBACK
  GitHub PAT provided:     $( [ -n "$GITHUB_PAT" ] && echo yes || echo no )
  AWS account:             $ACCOUNT_ID
  Dry run:                 $DRY_RUN

Your AWS credentials need at least:
  iam:GetRole/CreateRole/UpdateAssumeRolePolicy/PutRolePolicy/TagRole/PassRole
  secretsmanager:CreateSecret/PutSecretValue/DescribeSecret/TagResource (if a PAT is given)
  codebuild:BatchGetProjects/CreateProject/UpdateProject/StartBuild/BatchGetBuilds
  cloudformation:DescribeStacks, ssm:GetParameter
  bedrock:GetFoundationModelAvailability/GetInferenceProfile
  (plus the CDK bootstrap permissions, only if CDKToolkit is not yet bootstrapped)
SUMMARY

if [ "$ASSUME_YES" != "true" ]; then
  CONFIRM=""
  read -rp "Proceed with this configuration? [y/N]: " CONFIRM < /dev/tty
  case "$CONFIRM" in
    [Yy]|[Yy][Ee][Ss]) ;;
    *) echo "Aborted."; exit 1 ;;
  esac
fi

# ---------------------------------------------------------------------------
# Step 3 — write cei.deploy.json, npm ci, pre-flight (fail fast, no AWS writes yet)
# ---------------------------------------------------------------------------
write_config() {
  CEI_REGION="$REGION" \
  CEI_DEFAULT_MODEL="$DEFAULT_MODEL" \
  CEI_ROUTER_MODEL="$ROUTER_MODEL" \
  CEI_BLOCKED_MODELS="$BLOCKED_MODELS" \
  CEI_EMAIL_DOMAINS="$EMAIL_DOMAINS" \
  CEI_ADMIN_EMAIL="$ADMIN_EMAIL" \
  CEI_RPS="$RPS" \
  CEI_MAX_REALTIME="$MAX_REALTIME" \
  CEI_MAX_BACKGROUND="$MAX_BACKGROUND" \
  CEI_REPO_URL="$REPO_URL" \
  CEI_BRANCH="$BRANCH" \
  CEI_ALLOW_LOCALHOST_CALLBACK="$ALLOW_LOCALHOST_CALLBACK" \
  node -e '
    const fs = require("fs");
    const splitList = (v) => (v || "").split(",").map((s) => s.trim()).filter(Boolean);
    const cfg = {
      region: process.env.CEI_REGION,
      defaultModelId: process.env.CEI_DEFAULT_MODEL,
      routerModelId: process.env.CEI_ROUTER_MODEL,
      blockedModelIds: splitList(process.env.CEI_BLOCKED_MODELS),
      allowedEmailDomains: splitList(process.env.CEI_EMAIL_DOMAINS),
      adminEmail: process.env.CEI_ADMIN_EMAIL,
      limits: {
        requestsPerSecond: Number(process.env.CEI_RPS),
        maxRealtimeRunsPerUser: Number(process.env.CEI_MAX_REALTIME),
        maxBackgroundRunsPerUser: Number(process.env.CEI_MAX_BACKGROUND)
      },
      webSearchEnabled: false,
      allowLocalhostCallback: process.env.CEI_ALLOW_LOCALHOST_CALLBACK === "true",
      repository: {
        url: process.env.CEI_REPO_URL,
        branch: process.env.CEI_BRANCH
      }
    };
    fs.writeFileSync(process.argv[1], JSON.stringify(cfg, null, 2) + "\n");
  ' "$CONFIG_FILE"
}

write_config
echo "Wrote $CONFIG_FILE"

echo "Installing backend dependencies (npm ci)..."
( cd "$BACKEND_DIR" && npm ci )

echo "Running Region pre-flight check..."
set +e
( cd "$BACKEND_DIR" && npx ts-node scripts/preflight.ts --config "$(basename "$CONFIG_FILE")" --write )
PREFLIGHT_STATUS=$?
set -e
if [ "$PREFLIGHT_STATUS" -ne 0 ]; then
  echo "Pre-flight check failed (exit $PREFLIGHT_STATUS). See errors above. Stopping before any CodeBuild or CDK action." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Step 4 — dry run stops here
# ---------------------------------------------------------------------------
if [ "$DRY_RUN" = "true" ]; then
  cat <<EOF

Dry run complete: pre-flight checks passed and no AWS resources were created.
A real run would create/update:
  - IAM role:              $ROLE_NAME
  - CodeBuild project:     $PROJECT_NAME (source $REPO_URL@$BRANCH, image $BUILD_IMAGE)
  - CodeBuild GitHub source credentials (only if a PAT was supplied)
  - CloudFormation stack CDKToolkit in $REGION (only if not already bootstrapped)
EOF
  exit 0
fi

# ---------------------------------------------------------------------------
# Step 5 — CDK bootstrap
# ---------------------------------------------------------------------------
if ! aws cloudformation describe-stacks --stack-name CDKToolkit --region "$REGION" >/dev/null 2>&1; then
  echo "CDKToolkit not found in $REGION — bootstrapping..."
  ( cd "$BACKEND_DIR" && npx cdk bootstrap "aws://$ACCOUNT_ID/$REGION" )
else
  echo "CDKToolkit already present in $REGION."
fi

# ADR: also bootstrap us-east-1 | Rationale: B88 — EdgeWafStack (the CLOUDFRONT-scope WAF web ACL)
# always deploys to us-east-1 regardless of the chosen Region
if [ "$REGION" != "us-east-1" ]; then
  if ! aws cloudformation describe-stacks --stack-name CDKToolkit --region us-east-1 >/dev/null 2>&1; then
    echo "CDKToolkit not found in us-east-1 — bootstrapping..."
    ( cd "$BACKEND_DIR" && npx cdk bootstrap "aws://$ACCOUNT_ID/us-east-1" )
  else
    echo "CDKToolkit already present in us-east-1."
  fi
fi

# ---------------------------------------------------------------------------
# Step 8 (done before the IAM role so its policy can reference the secret) —
# GitHub PAT handling. The PAT is stored as a project-level Secrets Manager
# credential referenced by the CodeBuild project's own `source.auth` field
# (https://docs.aws.amazon.com/codebuild/latest/APIReference/API_SourceAuth.html)
# rather than through `import-source-credentials`, which would overwrite the
# single account+Region default GitHub credential shared by every other
# CodeBuild project. The secret value format and tags follow AWS's own
# convention for CodeBuild source credentials
# (https://docs.aws.amazon.com/codebuild/latest/userguide/asm-create-secret.html).
# The token never touches argv or an env var: it is piped over stdin into a
# small Node script that writes the JSON secret body to a 600-permission temp
# file, which is only ever referenced by its path and is deleted by the EXIT
# trap.
# ---------------------------------------------------------------------------
GITHUB_SECRET_ARN=""
OWNER_TAG_KEY="cei:owner"
OWNER_TAG_VALUE="deploy.sh"

if [ -n "$GITHUB_PAT" ]; then
  mktemp_secure pat_file
  printf '%s' "$GITHUB_PAT" | node -e '
    const fs = require("fs");
    let data = "";
    process.stdin.on("data", (chunk) => { data += chunk; });
    process.stdin.on("end", () => {
      const secret = { ServerType: "GITHUB", AuthType: "PERSONAL_ACCESS_TOKEN", Token: data };
      fs.writeFileSync(process.argv[1], JSON.stringify(secret));
    });
  ' "$pat_file"
  GITHUB_PAT=""

  echo "Storing the GitHub PAT in Secrets Manager ($GITHUB_PAT_SECRET_NAME) in $REGION..."
  # ADR: JSON-form tags (not shorthand) | Rationale: the required empty-valued codebuild:source
  # tag is unambiguous in JSON; the CLI's shorthand comma-splitting is not guaranteed to be
  GITHUB_SECRET_TAGS='[{"Key":"codebuild:source","Value":""},{"Key":"codebuild:source:type","Value":"personal_access_token"},{"Key":"codebuild:source:provider","Value":"github"},{"Key":"cei:owner","Value":"deploy.sh"}]'
  if aws secretsmanager describe-secret --secret-id "$GITHUB_PAT_SECRET_NAME" --region "$REGION" >/dev/null 2>&1; then
    EXISTING_SECRET_TAG="$(aws secretsmanager describe-secret --secret-id "$GITHUB_PAT_SECRET_NAME" --region "$REGION" --query "Tags[?Key=='$OWNER_TAG_KEY'].Value" --output text)"
    if [ "$EXISTING_SECRET_TAG" != "$OWNER_TAG_VALUE" ]; then
      rm -f "$pat_file"
      fail "Secret $GITHUB_PAT_SECRET_NAME exists but is not tagged $OWNER_TAG_KEY=$OWNER_TAG_VALUE; refusing to overwrite a resource this script does not own."
    fi
    GITHUB_SECRET_ARN="$(aws secretsmanager put-secret-value --secret-id "$GITHUB_PAT_SECRET_NAME" --region "$REGION" --secret-string "file://$pat_file" --query 'ARN' --output text)"
  else
    GITHUB_SECRET_ARN="$(aws secretsmanager create-secret --name "$GITHUB_PAT_SECRET_NAME" --region "$REGION" \
      --description "GitHub PAT for the $PROJECT_NAME CodeBuild project source access (B52: AWS-managed key)" \
      --secret-string "file://$pat_file" \
      --tags "$GITHUB_SECRET_TAGS" \
      --query 'ARN' --output text)"
  fi
  # ADR: delete the PAT temp file the moment Secrets Manager has it | Rationale: shrinks the
  # window the plaintext token exists on disk, ahead of the general EXIT/INT/TERM cleanup
  rm -f "$pat_file"
fi

# ---------------------------------------------------------------------------
# Step 6 — create/update the least-privilege CodeBuild service role
# ---------------------------------------------------------------------------
mktemp_secure trust_file
cat > "$trust_file" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "Service": "codebuild.amazonaws.com" },
      "Action": "sts:AssumeRole",
      "Condition": {
        "StringEquals": { "aws:SourceAccount": "$ACCOUNT_ID" },
        "ArnLike": { "aws:SourceArn": "arn:$PARTITION:codebuild:$REGION:$ACCOUNT_ID:project/$PROJECT_NAME" }
      }
    }
  ]
}
EOF

secret_statement=""
if [ -n "$GITHUB_SECRET_ARN" ]; then
  secret_statement=",{\"Sid\":\"ReadGithubPatSecret\",\"Effect\":\"Allow\",\"Action\":\"secretsmanager:GetSecretValue\",\"Resource\":\"$GITHUB_SECRET_ARN\"}"
fi

# ADR: widen to include us-east-1 | Rationale: B88 — EdgeWafStack always deploys to us-east-1, so
# the CodeBuild role needs the same cdk-hnb659fds-* AssumeRole (and bootstrap-version read) there too,
# in addition to whatever $REGION the rest of BackendStack deploys to
ASSUME_ROLE_RESOURCES="\"arn:$PARTITION:iam::$ACCOUNT_ID:role/cdk-hnb659fds-*-$ACCOUNT_ID-$REGION\""
BOOTSTRAP_VERSION_RESOURCES="\"arn:$PARTITION:ssm:$REGION:$ACCOUNT_ID:parameter/cdk-bootstrap/hnb659fds/version\""
if [ "$REGION" != "us-east-1" ]; then
  ASSUME_ROLE_RESOURCES="$ASSUME_ROLE_RESOURCES,\"arn:$PARTITION:iam::$ACCOUNT_ID:role/cdk-hnb659fds-*-$ACCOUNT_ID-us-east-1\""
  BOOTSTRAP_VERSION_RESOURCES="$BOOTSTRAP_VERSION_RESOURCES,\"arn:$PARTITION:ssm:us-east-1:$ACCOUNT_ID:parameter/cdk-bootstrap/hnb659fds/version\""
fi

mktemp_secure policy_file
cat > "$policy_file" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AssumeCdkBootstrapRoles",
      "Effect": "Allow",
      "Action": "sts:AssumeRole",
      "Resource": [$ASSUME_ROLE_RESOURCES],
      "Condition": {
        "StringEquals": {
          "iam:ResourceTag/aws-cdk:bootstrap-role": ["deploy", "file-publishing", "image-publishing", "lookup"]
        }
      }
    },
    {
      "Sid": "ReadCdkBootstrapVersion",
      "Effect": "Allow",
      "Action": "ssm:GetParameter",
      "Resource": [
        $BOOTSTRAP_VERSION_RESOURCES,
        "arn:$PARTITION:ssm:$REGION::parameter/aws/service/global-infrastructure/*"
      ]
    },
    {
      "Sid": "BedrockModelAvailabilityCheck",
      "Effect": "Allow",
      "Action": ["bedrock:GetFoundationModelAvailability", "bedrock:GetInferenceProfile"],
      "Resource": "*"
    },
    {
      "Sid": "CodeBuildOwnLogs",
      "Effect": "Allow",
      "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
      "Resource": [
        "arn:$PARTITION:logs:$REGION:$ACCOUNT_ID:log-group:/aws/codebuild/$PROJECT_NAME",
        "arn:$PARTITION:logs:$REGION:$ACCOUNT_ID:log-group:/aws/codebuild/$PROJECT_NAME:*"
      ]
    }$secret_statement
  ]
}
EOF
# ADR: bedrock:Get* actions above use Resource "*" | Rationale: these two actions have no resource-level ARN format in the Bedrock API — a specific resource ARN is rejected

if aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  EXISTING_ROLE_TAG="$(aws iam list-role-tags --role-name "$ROLE_NAME" --query "Tags[?Key=='$OWNER_TAG_KEY'].Value" --output text)"
  [ "$EXISTING_ROLE_TAG" = "$OWNER_TAG_VALUE" ] || fail "IAM role $ROLE_NAME exists but is not tagged $OWNER_TAG_KEY=$OWNER_TAG_VALUE; refusing to modify a resource this script does not own."
  echo "Updating IAM role $ROLE_NAME..."
  aws iam update-assume-role-policy --role-name "$ROLE_NAME" --policy-document "file://$trust_file"
else
  echo "Creating IAM role $ROLE_NAME..."
  aws iam create-role --role-name "$ROLE_NAME" \
    --assume-role-policy-document "file://$trust_file" \
    --description "CodeBuild service role for the $PROJECT_NAME deployment pipeline (owned by deploy.sh)" \
    --tags "Key=$OWNER_TAG_KEY,Value=$OWNER_TAG_VALUE"
fi
aws iam put-role-policy --role-name "$ROLE_NAME" --policy-name "$ROLE_POLICY_NAME" --policy-document "file://$policy_file"
ROLE_ARN="$(aws iam get-role --role-name "$ROLE_NAME" --query 'Role.Arn' --output text)"

echo "Waiting for IAM propagation..."
sleep 10

# ---------------------------------------------------------------------------
# Step 7 — create/update the CodeBuild project
# ---------------------------------------------------------------------------
CONFIG_B64="$(base64 < "$CONFIG_FILE" | tr -d '\n')"

if aws codebuild batch-get-projects --names "$PROJECT_NAME" --region "$REGION" --query 'projects[0].name' --output text 2>/dev/null | grep -qx "$PROJECT_NAME"; then
  EXISTING_PROJECT_TAG="$(aws codebuild batch-get-projects --names "$PROJECT_NAME" --region "$REGION" --query "projects[0].tags[?key=='$OWNER_TAG_KEY'].value" --output text)"
  [ "$EXISTING_PROJECT_TAG" = "$OWNER_TAG_VALUE" ] || fail "CodeBuild project $PROJECT_NAME exists but is not tagged $OWNER_TAG_KEY=$OWNER_TAG_VALUE; refusing to overwrite a resource this script does not own."
  PROJECT_EXISTS="true"
else
  PROJECT_EXISTS="false"
fi

mktemp_secure project_json_file
CEI_PROJECT_NAME="$PROJECT_NAME" \
CEI_REPO_URL="$REPO_URL" \
CEI_BRANCH="$BRANCH" \
CEI_ROLE_ARN="$ROLE_ARN" \
CEI_IMAGE="$BUILD_IMAGE" \
CEI_CONFIG_B64="$CONFIG_B64" \
CEI_GITHUB_SECRET_ARN="$GITHUB_SECRET_ARN" \
CEI_OWNER_TAG_KEY="$OWNER_TAG_KEY" \
CEI_OWNER_TAG_VALUE="$OWNER_TAG_VALUE" \
node -e '
  const fs = require("fs");
  const source = {
    type: "GITHUB",
    location: process.env.CEI_REPO_URL,
    buildspec: "buildspec.yml",
    reportBuildStatus: false
  };
  // ADR: project-level source.auth (SECRETS_MANAGER), not import-source-credentials | Rationale: import-source-credentials overwrites the one account+Region default GitHub credential shared by every other CodeBuild project; public repo = no auth field at all
  if (process.env.CEI_GITHUB_SECRET_ARN) {
    source.auth = { type: "SECRETS_MANAGER", resource: process.env.CEI_GITHUB_SECRET_ARN };
  }
  const project = {
    name: process.env.CEI_PROJECT_NAME,
    description: "CEI Life Sciences Bot guided deployment (created by deploy.sh)",
    source: source,
    sourceVersion: process.env.CEI_BRANCH,
    artifacts: { type: "NO_ARTIFACTS" },
    environment: {
      type: "LINUX_CONTAINER",
      image: process.env.CEI_IMAGE,
      computeType: "BUILD_GENERAL1_SMALL",
      privilegedMode: false,
      environmentVariables: [
        { name: "CEI_DEPLOY_CONFIG", value: process.env.CEI_CONFIG_B64, type: "PLAINTEXT" }
      ]
    },
    serviceRole: process.env.CEI_ROLE_ARN,
    timeoutInMinutes: 60,
    tags: [{ key: process.env.CEI_OWNER_TAG_KEY, value: process.env.CEI_OWNER_TAG_VALUE }]
  };
  fs.writeFileSync(process.argv[1], JSON.stringify(project, null, 2));
' "$project_json_file"

if [ "$PROJECT_EXISTS" = "true" ]; then
  echo "Updating CodeBuild project $PROJECT_NAME..."
  aws codebuild update-project --region "$REGION" --cli-input-json "file://$project_json_file" >/dev/null
else
  echo "Creating CodeBuild project $PROJECT_NAME..."
  aws codebuild create-project --region "$REGION" --cli-input-json "file://$project_json_file" >/dev/null
fi

# ---------------------------------------------------------------------------
# Step 9 — start the build (pinned to $SOURCE_VERSION) and poll to completion
# ---------------------------------------------------------------------------
echo "Starting CodeBuild deployment at $SOURCE_VERSION..."
BUILD_ID="$(aws codebuild start-build --project-name "$PROJECT_NAME" --source-version "$SOURCE_VERSION" --region "$REGION" --query 'build.id' --output text)"
CONSOLE_URL="https://$REGION.console.aws.amazon.com/codesuite/codebuild/projects/$PROJECT_NAME/build/$BUILD_ID/log?region=$REGION"
echo "Build started: $BUILD_ID"
echo "Console log: $CONSOLE_URL"

LAST_PHASE=""
POLL_FAILURES=0
while true; do
  # ADR: tolerate up to 3 consecutive poll failures | Rationale: a transient network/API blip
  # should not abandon an otherwise healthy build
  if ! POLL_OUTPUT="$(aws codebuild batch-get-builds --ids "$BUILD_ID" --region "$REGION" --query 'builds[0].[buildStatus,currentPhase]' --output text 2>/dev/null)"; then
    POLL_FAILURES=$((POLL_FAILURES + 1))
    echo "Warning: batch-get-builds failed ($POLL_FAILURES/3 consecutive failures)." >&2
    if [ "$POLL_FAILURES" -ge 3 ]; then
      echo "batch-get-builds failed 3 times in a row. Check the build manually: $CONSOLE_URL" >&2
      exit 1
    fi
    sleep 15
    continue
  fi
  POLL_FAILURES=0
  read -r BUILD_STATUS CURRENT_PHASE <<< "$POLL_OUTPUT"
  if [ "$CURRENT_PHASE" != "$LAST_PHASE" ]; then
    echo "Phase: $CURRENT_PHASE"
    LAST_PHASE="$CURRENT_PHASE"
  fi
  case "$BUILD_STATUS" in
    IN_PROGRESS)
      sleep 15
      ;;
    SUCCEEDED)
      echo "Build succeeded."
      exit 0
      ;;
    *)
      echo "Build ended with status '$BUILD_STATUS' in phase '$CURRENT_PHASE'. See $CONSOLE_URL" >&2
      exit 1
      ;;
  esac
done
