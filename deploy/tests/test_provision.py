from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from cei_deploy import provision


ACCOUNT_ID = "123456789012"
PARTITION = "aws"
REGION = "us-west-2"


# ---------------------------------------------------------------------------
# Policy documents (expected JSON built the same way deploy.sh's heredocs did)
# ---------------------------------------------------------------------------
def test_trust_policy_matches_bash_template():
    policy = provision.build_trust_policy(ACCOUNT_ID, PARTITION, REGION, provision.PROJECT_NAME)
    assert policy == {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "codebuild.amazonaws.com"},
            "Action": "sts:AssumeRole",
            "Condition": {
                "StringEquals": {"aws:SourceAccount": ACCOUNT_ID},
                "ArnLike": {
                    "aws:SourceArn": "arn:aws:codebuild:us-west-2:123456789012:project/%s" % provision.PROJECT_NAME,
                },
            },
        }],
    }


def test_role_policy_non_us_east_1_widens_to_include_it():
    policy = provision.build_role_policy(ACCOUNT_ID, PARTITION, REGION, provision.PROJECT_NAME)
    assume = next(s for s in policy["Statement"] if s["Sid"] == "AssumeCdkBootstrapRoles")
    assert assume["Resource"] == [
        "arn:aws:iam::123456789012:role/cdk-hnb659fds-*-123456789012-us-west-2",
        "arn:aws:iam::123456789012:role/cdk-hnb659fds-*-123456789012-us-east-1",
    ]
    ssm = next(s for s in policy["Statement"] if s["Sid"] == "ReadCdkBootstrapVersion")
    assert ssm["Resource"] == [
        "arn:aws:ssm:us-west-2:123456789012:parameter/cdk-bootstrap/hnb659fds/version",
        "arn:aws:ssm:us-east-1:123456789012:parameter/cdk-bootstrap/hnb659fds/version",
        "arn:aws:ssm:us-west-2::parameter/aws/service/global-infrastructure/*",
    ]
    sids = [s["Sid"] for s in policy["Statement"]]
    assert sids == [
        "AssumeCdkBootstrapRoles", "ReadCdkBootstrapVersion", "DescribeStacks",
        "BedrockModelAvailabilityCheck", "CodeBuildOwnLogs",
    ]


def test_role_policy_us_east_1_region_does_not_duplicate():
    policy = provision.build_role_policy(ACCOUNT_ID, PARTITION, "us-east-1", provision.PROJECT_NAME)
    assume = next(s for s in policy["Statement"] if s["Sid"] == "AssumeCdkBootstrapRoles")
    assert assume["Resource"] == ["arn:aws:iam::123456789012:role/cdk-hnb659fds-*-123456789012-us-east-1"]


def test_role_policy_appends_secret_statement_when_pat_present():
    policy = provision.build_role_policy(
        ACCOUNT_ID, PARTITION, REGION, provision.PROJECT_NAME, github_secret_arn="arn:aws:secretsmanager:...:secret",
    )
    assert policy["Statement"][-1] == {
        "Sid": "ReadGithubPatSecret",
        "Effect": "Allow",
        "Action": "secretsmanager:GetSecretValue",
        "Resource": "arn:aws:secretsmanager:...:secret",
    }


def test_bootstrap_policy_covers_every_bootstrap_region():
    policy = provision.build_bootstrap_policy(ACCOUNT_ID, PARTITION, [REGION, "us-east-1"])
    sids = [s["Sid"] for s in policy["Statement"]]
    assert sids == [
        "CloudFormationBootstrapStack", "BootstrapIamRoles", "BootstrapAssetsBucket",
        "BootstrapContainerAssetsRepo", "BootstrapVersionParameter",
    ]
    cfn = next(s for s in policy["Statement"] if s["Sid"] == "CloudFormationBootstrapStack")
    assert cfn["Resource"] == [
        "arn:aws:cloudformation:us-west-2:123456789012:stack/CDKToolkit/*",
        "arn:aws:cloudformation:us-east-1:123456789012:stack/CDKToolkit/*",
    ]


# ---------------------------------------------------------------------------
# Owner-tag guards
# ---------------------------------------------------------------------------
def test_ensure_iam_role_refuses_untagged_existing_role():
    iam = Mock()
    iam.get_role.return_value = {"Role": {"Arn": "arn:aws:iam::123456789012:role/x"}}
    iam.list_role_tags.return_value = {"Tags": [{"Key": "other", "Value": "thing"}]}
    with pytest.raises(provision.ProvisionError):
        provision.ensure_iam_role(iam, account_id=ACCOUNT_ID, partition=PARTITION, region=REGION)


def test_ensure_iam_role_creates_when_missing():
    iam = Mock()
    iam.get_role.side_effect = [Exception("not found"), {"Role": {"Arn": "arn:aws:iam::123456789012:role/new"}}]
    arn = provision.ensure_iam_role(iam, account_id=ACCOUNT_ID, partition=PARTITION, region=REGION)
    assert arn == "arn:aws:iam::123456789012:role/new"
    iam.create_role.assert_called_once()
    tags = iam.create_role.call_args.kwargs["Tags"]
    assert tags == [{"Key": "cei:owner", "Value": "deploy.sh"}]
    iam.put_role_policy.assert_called_once()


def test_ensure_github_pat_secret_refuses_untagged_existing_secret():
    sm = Mock()
    sm.describe_secret.return_value = {"ARN": "arn:x", "Tags": [{"Key": "other", "Value": "thing"}]}
    with pytest.raises(provision.ProvisionError):
        provision.ensure_github_pat_secret(sm, pat_token="ghp_abc", use_stored=False)


def test_ensure_github_pat_secret_creates_new():
    sm = Mock()
    sm.describe_secret.side_effect = Exception("not found")
    sm.create_secret.return_value = {"ARN": "arn:new-secret"}
    arn = provision.ensure_github_pat_secret(sm, pat_token="ghp_abc", use_stored=False)
    assert arn == "arn:new-secret"
    body = json.loads(sm.create_secret.call_args.kwargs["SecretString"])
    assert body == {"ServerType": "GITHUB", "AuthType": "PERSONAL_ACCESS_TOKEN", "Token": "ghp_abc"}


def test_ensure_github_pat_secret_uses_stored_when_no_token_given():
    sm = Mock()
    sm.describe_secret.return_value = {"ARN": "arn:stored"}
    arn = provision.ensure_github_pat_secret(sm, pat_token=None, use_stored=True)
    assert arn == "arn:stored"
    sm.create_secret.assert_not_called()
    sm.put_secret_value.assert_not_called()


def test_ensure_github_pat_secret_no_token_no_stored_returns_empty():
    sm = Mock()
    sm.describe_secret.side_effect = Exception("not found")
    assert provision.ensure_github_pat_secret(sm, pat_token=None, use_stored=False) == ""


def test_ensure_codebuild_project_refuses_untagged_existing_project():
    cb = Mock()
    cb.batch_get_projects.return_value = {"projects": [{"name": provision.PROJECT_NAME, "tags": [{"key": "other", "value": "x"}]}]}
    with pytest.raises(provision.ProvisionError):
        provision.ensure_codebuild_project(
            cb, project_name=provision.PROJECT_NAME, repo_url="https://github.com/a/b", branch="main",
            role_arn="arn:x", config_b64="e30=",
        )


def test_ensure_codebuild_project_creates_when_missing():
    cb = Mock()
    cb.batch_get_projects.return_value = {"projects": []}
    provision.ensure_codebuild_project(
        cb, project_name=provision.PROJECT_NAME, repo_url="https://github.com/a/b", branch="main",
        role_arn="arn:x", config_b64="e30=",
    )
    cb.create_project.assert_called_once()
    assert cb.create_project.call_args.kwargs["source"]["location"] == "https://github.com/a/b"
    assert "auth" not in cb.create_project.call_args.kwargs["source"]


def test_project_definition_sets_auth_only_with_secret():
    definition = provision.build_project_definition(
        project_name="p", repo_url="https://github.com/a/b", branch="main", role_arn="arn:x", config_b64="e30=",
        github_secret_arn="arn:secret",
    )
    assert definition["source"]["auth"] == {"type": "SECRETS_MANAGER", "resource": "arn:secret"}


def test_check_leftover_spike_stacks():
    cfn = Mock()

    def describe(StackName):
        if StackName == "ToolScopeSpikeStack":
            return {}
        raise Exception("does not exist")

    cfn.describe_stacks.side_effect = describe
    assert provision.check_leftover_spike_stacks(cfn) == ["ToolScopeSpikeStack"]


def test_determine_bootstrap_regions_dedupes_and_checks_once():
    calls = []

    def factory(region):
        calls.append(region)
        client = Mock()
        client.describe_stacks.side_effect = Exception("missing")
        return client

    regions = provision.determine_bootstrap_regions(factory, "us-east-1")
    assert calls == ["us-east-1"]
    assert regions == ["us-east-1"]


def test_determine_bootstrap_regions_checks_both_when_different():
    def factory(region):
        client = Mock()
        if region == "us-east-1":
            client.describe_stacks.return_value = {}
        else:
            client.describe_stacks.side_effect = Exception("missing")
        return client

    regions = provision.determine_bootstrap_regions(factory, "us-west-2")
    assert regions == ["us-west-2"]


# ---------------------------------------------------------------------------
# start_and_poll_build
# ---------------------------------------------------------------------------
def test_start_and_poll_build_succeeds():
    cb = Mock()
    cb.start_build.return_value = {"build": {"id": "build-1"}}
    cb.batch_get_builds.return_value = {"builds": [{"buildStatus": "SUCCEEDED", "currentPhase": "COMPLETED"}]}
    sleeps = []
    result = provision.start_and_poll_build(
        cb, region=REGION, project_name=provision.PROJECT_NAME, source_version="abc123",
        sleep_fn=sleeps.append,
    )
    assert result.status == "SUCCEEDED"
    assert result.build_id == "build-1"
    assert sleeps == []
    assert cb.start_build.call_args.kwargs == {"projectName": provision.PROJECT_NAME, "sourceVersion": "abc123"}


def test_start_and_poll_build_tolerates_transient_failures():
    cb = Mock()
    cb.start_build.return_value = {"build": {"id": "build-1"}}
    cb.batch_get_builds.side_effect = [
        Exception("blip"),
        {"builds": [{"buildStatus": "SUCCEEDED", "currentPhase": "COMPLETED"}]},
    ]
    sleeps = []
    result = provision.start_and_poll_build(
        cb, region=REGION, project_name=provision.PROJECT_NAME, source_version="abc123", sleep_fn=sleeps.append,
    )
    assert result.status == "SUCCEEDED"
    assert sleeps == [15.0]


def test_start_and_poll_build_gives_up_after_max_failures():
    cb = Mock()
    cb.start_build.return_value = {"build": {"id": "build-1"}}
    cb.batch_get_builds.side_effect = Exception("down")
    sleeps = []
    result = provision.start_and_poll_build(
        cb, region=REGION, project_name=provision.PROJECT_NAME, source_version="abc123",
        sleep_fn=sleeps.append, max_consecutive_failures=3,
    )
    assert result.status == "UNKNOWN"
    assert len(sleeps) == 2


def test_start_and_poll_build_never_sends_spike_overrides():
    cb = Mock()
    cb.start_build.return_value = {"build": {"id": "build-1"}}
    cb.batch_get_builds.return_value = {"builds": [{"buildStatus": "SUCCEEDED", "currentPhase": "DONE"}]}
    provision.start_and_poll_build(
        cb, region=REGION, project_name=provision.PROJECT_NAME, source_version="abc123",
    )
    assert "environmentVariablesOverride" not in cb.start_build.call_args.kwargs
