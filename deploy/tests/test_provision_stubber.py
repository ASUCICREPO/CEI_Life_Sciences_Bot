"""A botocore Stubber-based test, as an alternative to the Mock-based fakes in test_provision.py."""
from __future__ import annotations

from datetime import datetime, timezone

import boto3
from botocore.stub import Stubber

from cei_deploy import provision


def test_check_cdk_toolkit_with_stubber_present():
    client = boto3.client("cloudformation", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="y")
    with Stubber(client) as stubber:
        stubber.add_response("describe_stacks", {
            "Stacks": [{
                "StackName": "CDKToolkit",
                "CreationTime": datetime.now(timezone.utc),
                "StackStatus": "CREATE_COMPLETE",
            }],
        })
        assert provision.check_cdk_toolkit(client) is True


def test_check_cdk_toolkit_with_stubber_missing():
    client = boto3.client("cloudformation", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="y")
    with Stubber(client) as stubber:
        stubber.add_client_error("describe_stacks", service_error_code="ValidationError")
        assert provision.check_cdk_toolkit(client) is False
