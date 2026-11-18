"""boto3 port of deploy.sh steps 5-9: the CodeBuild deployment pipeline itself.

Every function here takes an injected boto3 client so tests can pass a `botocore.stub.Stubber` or a
small fake instead of talking to AWS. `deploy()` is the orchestration entry point the TUI's
provisioning worker calls; every other function is independently unit-testable.
"""
from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

LogFn = Callable[[str], None]


def _noop_log(_message: str) -> None:
    return None


ROLE_NAME = "cei-deploy-codebuild-role"
ROLE_POLICY_NAME = "cei-deploy-codebuild-policy"
BOOTSTRAP_POLICY_NAME = "cei-deploy-bootstrap"
PROJECT_NAME = "cei-life-sciences-deploy"
GITHUB_PAT_SECRET_NAME = "cei/deploy/github-pat"
# ADR: standard:8.0 (Ubuntu 24.04) is the current CodeBuild-recommended image | re-verify against AWS
# docs if CodeBuild's recommended image has moved on (copied from deploy.sh)
BUILD_IMAGE = "aws/codebuild/standard:8.0"
OWNER_TAG_KEY = "cei:owner"
# ADR: keep the tag value "deploy.sh" | Rationale: existing IAM role/secret/project created by the
# old bash deploy.sh are already tagged cei:owner=deploy.sh; this tool is invoked through deploy.sh
# (now a thin wrapper), so resources it owns must keep passing that same owner-tag guard
OWNER_TAG_VALUE = "deploy.sh"

LEFTOVER_SPIKE_STACK_NAMES = (
    "ToolScopeSpikeStack", "StreamingSpikeStack", "DurableSpikeStack", "IdentitySpikeStack",
)


class ProvisionError(Exception):
    """A step refused to proceed (e.g. an owner-tag guard failed) or AWS returned an error."""


@dataclass
class BuildResult:
    status: str
    build_id: str
    console_url: str


# ---------------------------------------------------------------------------
# Step 5 — CDK bootstrap check (read-only)
# ---------------------------------------------------------------------------
def check_cdk_toolkit(cfn_client: Any) -> bool:
    try:
        cfn_client.describe_stacks(StackName="CDKToolkit")
        return True
    except Exception:
        return False


def determine_bootstrap_regions(
    cfn_client_factory: Callable[[str], Any], region: str, log: LogFn = _noop_log,
) -> List[str]:
    # ADR: dedupe region/us-east-1 with a "seen" list | Rationale: B88 — EdgeWafStack always deploys
    # to us-east-1, which may equal `region`; without dedup a second already-bootstrapped region
    # would be re-checked (copied from deploy.sh)
    seen: List[str] = []
    bootstrap_regions: List[str] = []
    for r in (region, "us-east-1"):
        if r in seen:
            continue
        seen.append(r)
        if check_cdk_toolkit(cfn_client_factory(r)):
            log("CDKToolkit already present in %s." % r)
        else:
            log("CDKToolkit not found in %s — will bootstrap it." % r)
            bootstrap_regions.append(r)
    return bootstrap_regions


def check_leftover_spike_stacks(cfn_client: Any) -> List[str]:
    found = []
    for name in LEFTOVER_SPIKE_STACK_NAMES:
        try:
            cfn_client.describe_stacks(StackName=name)
            found.append(name)
        except Exception:
            continue
    return found


# ---------------------------------------------------------------------------
# Step 8 — GitHub PAT handling (done before the IAM role so its policy can reference the secret)
# ---------------------------------------------------------------------------
def describe_github_pat_secret(secretsmanager_client: Any) -> Optional[str]:
    """Existing secret ARN, or None if it does not exist."""
    try:
        response = secretsmanager_client.describe_secret(SecretId=GITHUB_PAT_SECRET_NAME)
    except Exception:
        return None
    return response.get("ARN")


def _secret_tags(tags: Any) -> Dict[str, str]:
    return {t.get("Key"): t.get("Value") for t in (tags or []) if isinstance(t, dict)}


def ensure_github_pat_secret(
    secretsmanager_client: Any, *, pat_token: Optional[str], use_stored: bool, log: LogFn = _noop_log,
) -> str:
    """Returns the secret ARN, or "" if no PAT is involved. Never logs or persists the token."""
    if not pat_token:
        if use_stored:
            arn = describe_github_pat_secret(secretsmanager_client)
            if arn:
                log("Using existing GitHub PAT from Secrets Manager.")
                return arn
        return ""

    secret_string = json.dumps({"ServerType": "GITHUB", "AuthType": "PERSONAL_ACCESS_TOKEN", "Token": pat_token})
    log("Storing the GitHub PAT in Secrets Manager (%s)..." % GITHUB_PAT_SECRET_NAME)
    tags = [
        {"Key": "codebuild:source", "Value": ""},
        {"Key": "codebuild:source:type", "Value": "personal_access_token"},
        {"Key": "codebuild:source:provider", "Value": "github"},
        {"Key": OWNER_TAG_KEY, "Value": OWNER_TAG_VALUE},
    ]
    try:
        existing = secretsmanager_client.describe_secret(SecretId=GITHUB_PAT_SECRET_NAME)
    except Exception:
        existing = None

    if existing is not None:
        existing_tag = _secret_tags(existing.get("Tags")).get(OWNER_TAG_KEY)
        if existing_tag != OWNER_TAG_VALUE:
            raise ProvisionError(
                "Secret %s exists but is not tagged %s=%s; refusing to overwrite a resource this "
                "tool does not own." % (GITHUB_PAT_SECRET_NAME, OWNER_TAG_KEY, OWNER_TAG_VALUE)
            )
        response = secretsmanager_client.put_secret_value(
            SecretId=GITHUB_PAT_SECRET_NAME, SecretString=secret_string,
        )
    else:
        response = secretsmanager_client.create_secret(
            Name=GITHUB_PAT_SECRET_NAME,
            Description="GitHub PAT for the %s CodeBuild project source access (AWS-managed key)" % PROJECT_NAME,
            SecretString=secret_string,
            Tags=tags,
        )
    return response["ARN"]


# ---------------------------------------------------------------------------
# Step 6 — IAM role + inline policy
# ---------------------------------------------------------------------------
def build_trust_policy(account_id: str, partition: str, region: str, project_name: str = PROJECT_NAME) -> Dict:
    return {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "codebuild.amazonaws.com"},
            "Action": "sts:AssumeRole",
            "Condition": {
                "StringEquals": {"aws:SourceAccount": account_id},
                "ArnLike": {
                    "aws:SourceArn": "arn:%s:codebuild:%s:%s:project/%s" % (partition, region, account_id, project_name)
                },
            },
        }],
    }


def build_role_policy(
    account_id: str, partition: str, region: str, project_name: str = PROJECT_NAME,
    github_secret_arn: str = "",
) -> Dict:
    assume_role_resources = ["arn:%s:iam::%s:role/cdk-hnb659fds-*-%s-%s" % (partition, account_id, account_id, region)]
    bootstrap_version_resources = [
        "arn:%s:ssm:%s:%s:parameter/cdk-bootstrap/hnb659fds/version" % (partition, region, account_id),
    ]
    # ADR: widen to include us-east-1 | B88 — EdgeWafStack always deploys to us-east-1
    if region != "us-east-1":
        assume_role_resources.append(
            "arn:%s:iam::%s:role/cdk-hnb659fds-*-%s-us-east-1" % (partition, account_id, account_id)
        )
        bootstrap_version_resources.append(
            "arn:%s:ssm:us-east-1:%s:parameter/cdk-bootstrap/hnb659fds/version" % (partition, account_id)
        )

    statements: List[Dict[str, Any]] = [
        {
            "Sid": "AssumeCdkBootstrapRoles",
            "Effect": "Allow",
            "Action": "sts:AssumeRole",
            "Resource": assume_role_resources,
            "Condition": {
                "StringEquals": {
                    "iam:ResourceTag/aws-cdk:bootstrap-role": ["deploy", "file-publishing", "image-publishing", "lookup"],
                },
            },
        },
        {
            "Sid": "ReadCdkBootstrapVersion",
            "Effect": "Allow",
            "Action": "ssm:GetParameter",
            "Resource": bootstrap_version_resources + [
                "arn:%s:ssm:%s::parameter/aws/service/global-infrastructure/*" % (partition, region),
            ],
        },
        {
            "Sid": "DescribeStacks",
            "Effect": "Allow",
            "Action": "cloudformation:DescribeStacks",
            "Resource": "*",
        },
        {
            "Sid": "BedrockModelAvailabilityCheck",
            "Effect": "Allow",
            "Action": ["bedrock:GetFoundationModelAvailability", "bedrock:GetInferenceProfile"],
            "Resource": "*",
        },
        {
            "Sid": "CodeBuildOwnLogs",
            "Effect": "Allow",
            "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
            "Resource": [
                "arn:%s:logs:%s:%s:log-group:/aws/codebuild/%s" % (partition, region, account_id, project_name),
                "arn:%s:logs:%s:%s:log-group:/aws/codebuild/%s:*" % (partition, region, account_id, project_name),
            ],
        },
    ]
    if github_secret_arn:
        statements.append({
            "Sid": "ReadGithubPatSecret",
            "Effect": "Allow",
            "Action": "secretsmanager:GetSecretValue",
            "Resource": github_secret_arn,
        })
    return {"Version": "2012-10-17", "Statement": statements}


def _role_tags(tags: Any) -> Dict[str, str]:
    return {t.get("Key"): t.get("Value") for t in (tags or []) if isinstance(t, dict)}


def ensure_iam_role(
    iam_client: Any, *, account_id: str, partition: str, region: str, github_secret_arn: str = "",
    project_name: str = PROJECT_NAME, log: LogFn = _noop_log,
) -> str:
    trust_policy = build_trust_policy(account_id, partition, region, project_name)
    role_policy = build_role_policy(account_id, partition, region, project_name, github_secret_arn)

    try:
        iam_client.get_role(RoleName=ROLE_NAME)
        exists = True
    except Exception:
        exists = False

    if exists:
        tags_response = iam_client.list_role_tags(RoleName=ROLE_NAME)
        existing_tag = _role_tags(tags_response.get("Tags")).get(OWNER_TAG_KEY)
        if existing_tag != OWNER_TAG_VALUE:
            raise ProvisionError(
                "IAM role %s exists but is not tagged %s=%s; refusing to modify a resource this "
                "tool does not own." % (ROLE_NAME, OWNER_TAG_KEY, OWNER_TAG_VALUE)
            )
        log("Updating IAM role %s..." % ROLE_NAME)
        iam_client.update_assume_role_policy(RoleName=ROLE_NAME, PolicyDocument=json.dumps(trust_policy))
    else:
        log("Creating IAM role %s..." % ROLE_NAME)
        iam_client.create_role(
            RoleName=ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(trust_policy),
            Description="CodeBuild service role for the %s deployment pipeline (owned by deploy.sh)" % project_name,
            Tags=[{"Key": OWNER_TAG_KEY, "Value": OWNER_TAG_VALUE}],
        )

    iam_client.put_role_policy(
        RoleName=ROLE_NAME, PolicyName=ROLE_POLICY_NAME, PolicyDocument=json.dumps(role_policy),
    )
    return iam_client.get_role(RoleName=ROLE_NAME)["Role"]["Arn"]


# ---------------------------------------------------------------------------
# Step 6b — the one-time "cei-deploy-bootstrap" inline policy
# ---------------------------------------------------------------------------
def build_bootstrap_policy(account_id: str, partition: str, regions: List[str]) -> Dict:
    def for_each_region(fn: Callable[[str], str]) -> List[str]:
        return [fn(r) for r in regions]

    statements = [
        {
            "Sid": "CloudFormationBootstrapStack",
            "Effect": "Allow",
            "Action": [
                "cloudformation:CreateStack", "cloudformation:UpdateStack", "cloudformation:DeleteStack",
                "cloudformation:DescribeStacks", "cloudformation:DescribeStackEvents",
                "cloudformation:GetTemplate", "cloudformation:GetTemplateSummary",
                "cloudformation:CreateChangeSet", "cloudformation:DescribeChangeSet",
                "cloudformation:ExecuteChangeSet", "cloudformation:DeleteChangeSet",
                "cloudformation:RollbackStack", "cloudformation:ContinueUpdateRollback",
            ],
            "Resource": for_each_region(lambda r: "arn:%s:cloudformation:%s:%s:stack/CDKToolkit/*" % (partition, r, account_id)),
        },
        {
            "Sid": "BootstrapIamRoles",
            "Effect": "Allow",
            "Action": [
                "iam:CreateRole", "iam:GetRole", "iam:DeleteRole", "iam:UpdateRole",
                "iam:UpdateAssumeRolePolicy", "iam:PutRolePolicy", "iam:GetRolePolicy",
                "iam:DeleteRolePolicy", "iam:ListRolePolicies", "iam:AttachRolePolicy",
                "iam:DetachRolePolicy", "iam:ListAttachedRolePolicies", "iam:TagRole",
                "iam:UntagRole", "iam:ListRoleTags", "iam:PassRole",
            ],
            "Resource": for_each_region(lambda r: "arn:%s:iam::%s:role/cdk-hnb659fds-*-%s-%s" % (partition, account_id, account_id, r)),
        },
        {
            "Sid": "BootstrapAssetsBucket",
            "Effect": "Allow",
            "Action": [
                "s3:CreateBucket", "s3:PutBucketPolicy", "s3:GetBucketPolicy",
                "s3:PutBucketVersioning", "s3:GetBucketVersioning",
                "s3:PutEncryptionConfiguration", "s3:GetEncryptionConfiguration",
                "s3:PutLifecycleConfiguration", "s3:GetLifecycleConfiguration",
                "s3:PutBucketPublicAccessBlock", "s3:GetBucketPublicAccessBlock",
            ],
            "Resource": for_each_region(lambda r: "arn:%s:s3:::cdk-hnb659fds-assets-%s-%s" % (partition, account_id, r)),
        },
        {
            "Sid": "BootstrapContainerAssetsRepo",
            "Effect": "Allow",
            "Action": [
                "ecr:CreateRepository", "ecr:DescribeRepositories", "ecr:SetRepositoryPolicy",
                "ecr:GetRepositoryPolicy", "ecr:PutLifecyclePolicy", "ecr:GetLifecyclePolicy",
                "ecr:TagResource",
            ],
            "Resource": for_each_region(lambda r: "arn:%s:ecr:%s:%s:repository/cdk-hnb659fds-container-assets-%s-%s" % (partition, r, account_id, account_id, r)),
        },
        {
            "Sid": "BootstrapVersionParameter",
            "Effect": "Allow",
            "Action": ["ssm:PutParameter", "ssm:GetParameter", "ssm:AddTagsToResource"],
            "Resource": for_each_region(lambda r: "arn:%s:ssm:%s:%s:parameter/cdk-bootstrap/hnb659fds/*" % (partition, r, account_id)),
        },
    ]
    return {"Version": "2012-10-17", "Statement": statements}


def ensure_bootstrap_policy(iam_client: Any, *, account_id: str, partition: str, bootstrap_regions: List[str], log: LogFn = _noop_log) -> None:
    if bootstrap_regions:
        log("Attaching the one-time %s policy for: %s..." % (BOOTSTRAP_POLICY_NAME, ", ".join(bootstrap_regions)))
        policy = build_bootstrap_policy(account_id, partition, bootstrap_regions)
        iam_client.put_role_policy(RoleName=ROLE_NAME, PolicyName=BOOTSTRAP_POLICY_NAME, PolicyDocument=json.dumps(policy))
    else:
        log("Removing the %s policy, if present (no Region needs bootstrapping)..." % BOOTSTRAP_POLICY_NAME)
        try:
            iam_client.delete_role_policy(RoleName=ROLE_NAME, PolicyName=BOOTSTRAP_POLICY_NAME)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Step 7 — CodeBuild project
# ---------------------------------------------------------------------------
def _project_tags(tags: Any) -> Dict[str, str]:
    return {t.get("key"): t.get("value") for t in (tags or []) if isinstance(t, dict)}


def build_project_definition(
    *, project_name: str, repo_url: str, branch: str, role_arn: str, config_b64: str,
    github_secret_arn: str = "", image: str = BUILD_IMAGE,
) -> Dict[str, Any]:
    source: Dict[str, Any] = {
        "type": "GITHUB",
        "location": repo_url,
        "buildspec": "buildspec.yml",
        "reportBuildStatus": False,
    }
    # ADR: project-level source.auth (SECRETS_MANAGER), not import-source-credentials | Rationale:
    # import-source-credentials overwrites the one account+Region default GitHub credential shared
    # by every other CodeBuild project; a public repo gets no auth field at all (copied from deploy.sh)
    if github_secret_arn:
        source["auth"] = {"type": "SECRETS_MANAGER", "resource": github_secret_arn}

    return {
        "name": project_name,
        "description": "CEI Life Sciences Bot guided deployment (created by deploy.sh)",
        "source": source,
        "sourceVersion": branch,
        "artifacts": {"type": "NO_ARTIFACTS"},
        "environment": {
            "type": "LINUX_CONTAINER",
            "image": image,
            "computeType": "BUILD_GENERAL1_SMALL",
            "privilegedMode": False,
            "environmentVariables": [
                {"name": "CEI_DEPLOY_CONFIG", "value": config_b64, "type": "PLAINTEXT"},
            ],
        },
        "serviceRole": role_arn,
        "timeoutInMinutes": 60,
        "tags": [{"key": OWNER_TAG_KEY, "value": OWNER_TAG_VALUE}],
    }


def ensure_codebuild_project(
    cb_client: Any, *, project_name: str, repo_url: str, branch: str, role_arn: str, config_b64: str,
    github_secret_arn: str = "", log: LogFn = _noop_log,
) -> None:
    try:
        response = cb_client.batch_get_projects(names=[project_name])
        projects = response.get("projects") or []
    except Exception:
        projects = []

    exists = bool(projects)
    if exists:
        existing_tag = _project_tags(projects[0].get("tags")).get(OWNER_TAG_KEY)
        if existing_tag != OWNER_TAG_VALUE:
            raise ProvisionError(
                "CodeBuild project %s exists but is not tagged %s=%s; refusing to overwrite a "
                "resource this tool does not own." % (project_name, OWNER_TAG_KEY, OWNER_TAG_VALUE)
            )

    definition = build_project_definition(
        project_name=project_name, repo_url=repo_url, branch=branch, role_arn=role_arn,
        config_b64=config_b64, github_secret_arn=github_secret_arn,
    )
    if exists:
        log("Updating CodeBuild project %s..." % project_name)
        cb_client.update_project(**definition)
    else:
        log("Creating CodeBuild project %s..." % project_name)
        cb_client.create_project(**definition)


def config_to_base64(config_json: str) -> str:
    """Same bytes as `base64 < cei.deploy.json | tr -d '\\n'` in deploy.sh."""
    return base64.b64encode(config_json.encode("utf-8")).decode("ascii")


# ---------------------------------------------------------------------------
# Step 9 — start the build and poll to completion
# ---------------------------------------------------------------------------
def start_and_poll_build(
    cb_client: Any, *, region: str, project_name: str, source_version: str, log: LogFn = _noop_log,
    poll_interval_seconds: float = 15.0, max_consecutive_failures: int = 3,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> BuildResult:
    log("Starting CodeBuild deployment at %s..." % source_version)
    # ADR: no environmentVariablesOverride | Rationale: spike stacks are removed; a normal deploy
    # never needs a start-build override, unlike the old deploy.sh CEI_SPIKES/CEI_SPIKE_POLICY_MODE
    started = cb_client.start_build(projectName=project_name, sourceVersion=source_version)
    build_id = started["build"]["id"]
    console_url = "https://%s.console.aws.amazon.com/codesuite/codebuild/projects/%s/build/%s/log?region=%s" % (
        region, project_name, build_id, region,
    )
    log("Build started: %s" % build_id)
    log("Console log: %s" % console_url)

    last_phase = ""
    failures = 0
    while True:
        try:
            builds = cb_client.batch_get_builds(ids=[build_id])["builds"]
            status = builds[0]["buildStatus"]
            phase = builds[0].get("currentPhase", "")
        except Exception:
            failures += 1
            log("Warning: batch-get-builds failed (%d/%d consecutive failures)." % (failures, max_consecutive_failures))
            if failures >= max_consecutive_failures:
                log(
                    "batch-get-builds failed %d times in a row. Check the build manually: %s"
                    % (max_consecutive_failures, console_url)
                )
                return BuildResult(status="UNKNOWN", build_id=build_id, console_url=console_url)
            sleep_fn(poll_interval_seconds)
            continue

        failures = 0
        if phase != last_phase:
            log("Phase: %s" % phase)
            last_phase = phase

        if status == "IN_PROGRESS":
            sleep_fn(poll_interval_seconds)
            continue
        if status == "SUCCEEDED":
            log("Build succeeded.")
        else:
            log("Build ended with status '%s' in phase '%s'. See %s" % (status, phase, console_url))
        return BuildResult(status=status, build_id=build_id, console_url=console_url)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def deploy(
    *, session: Any, region: str, repo_url: str, branch: str, source_version: str, config_json: str,
    pat_token: Optional[str], use_stored_pat: bool, log: LogFn = _noop_log,
) -> BuildResult:
    """Full provisioning + build, mirroring deploy.sh steps 5-9. `session` is a boto3.Session."""
    sts = session.client("sts")
    identity = sts.get_caller_identity()
    account_id = identity["Account"]
    partition = identity["Arn"].split(":")[1]

    def cfn_client_for(r: str) -> Any:
        return session.client("cloudformation", region_name=r)

    bootstrap_regions = determine_bootstrap_regions(cfn_client_for, region, log)

    secretsmanager = session.client("secretsmanager", region_name=region)
    github_secret_arn = ensure_github_pat_secret(secretsmanager, pat_token=pat_token, use_stored=use_stored_pat, log=log)

    iam = session.client("iam")
    role_arn = ensure_iam_role(
        iam, account_id=account_id, partition=partition, region=region, github_secret_arn=github_secret_arn, log=log,
    )
    ensure_bootstrap_policy(iam, account_id=account_id, partition=partition, bootstrap_regions=bootstrap_regions, log=log)

    log("Waiting for IAM propagation...")
    time.sleep(10)

    config_b64 = config_to_base64(config_json)
    cb = session.client("codebuild", region_name=region)
    ensure_codebuild_project(
        cb, project_name=PROJECT_NAME, repo_url=repo_url, branch=branch, role_arn=role_arn,
        config_b64=config_b64, github_secret_arn=github_secret_arn, log=log,
    )

    return start_and_poll_build(cb, region=region, project_name=PROJECT_NAME, source_version=source_version, log=log)
