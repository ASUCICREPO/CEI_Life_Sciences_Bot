"""Publish a synthetic spike CloudFormation template; this never deploys a stack."""
from __future__ import annotations

import os
import re
from pathlib import Path

import boto3

region = os.environ.get("AWS_REGION")
revision = os.environ.get("CODEBUILD_RESOLVED_SOURCE_VERSION", "")
kind = os.environ.get("CEI_SPIKE_TYPE", "durable")
stacks = {"durable": "DurableSpikeStack", "identity": "IdentitySpikeStack"}
identity = boto3.client("sts", region_name=region).get_caller_identity()
account = identity["Account"]
if (region != "us-east-1" or account != "257137551728" or kind not in stacks
        or not re.fullmatch(r"[a-f0-9]{40}", revision)):
    raise RuntimeError("Unexpected account, region, or source revision")
template = Path(f"cdk.out/{stacks[kind]}.template.json").read_bytes()
if not template or len(template) > 1_048_576:
    raise RuntimeError("Invalid spike template")
role = f"arn:aws:iam::{account}:role/cdk-hnb659fds-file-publishing-role-{account}-{region}"
credentials = boto3.client("sts", region_name=region).assume_role(
    RoleArn=role, RoleSessionName="cei-durable-spike-template"
)["Credentials"]
s3 = boto3.client("s3", region_name=region,
                  aws_access_key_id=credentials["AccessKeyId"],
                  aws_secret_access_key=credentials["SecretAccessKey"],
                  aws_session_token=credentials["SessionToken"])
bucket = f"cdk-hnb659fds-assets-{account}-{region}"
key = f"cei-spikes/{kind}/{revision}/template.json"
s3.put_object(Bucket=bucket, Key=key, Body=template, ContentType="application/json", ServerSideEncryption="AES256")
print(f"SPIKE_TEMPLATE_URL=https://s3.{region}.amazonaws.com/{bucket}/{key}")
