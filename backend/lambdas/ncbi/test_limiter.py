import boto3
import pytest
from moto import mock_aws

from limiter import NCBIRateLimiter, RateLimited


@pytest.fixture
def limiter():
    with mock_aws():
        dynamo = boto3.client("dynamodb", region_name="us-east-1")
        dynamo.create_table(TableName="ncbi-limits", BillingMode="PAY_PER_REQUEST",
                            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
                            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}])
        clock = [1000.0]
        yield NCBIRateLimiter(dynamo, "ncbi-limits", clock=lambda: clock[0]), clock


def test_blast_global_and_per_rid_are_both_enforced(limiter):
    rate, clock = limiter
    rate.acquire("blast", "RID123")
    clock[0] += 10
    with pytest.raises(RateLimited):
        rate.acquire("blast", "RID123")
    # Failed RID reservation must not consume the global slot.
    rate.acquire("blast", "RID456")
    clock[0] += 50
    rate.acquire("blast", "RID123")


def test_eutils_is_globally_limited_and_fails_closed(limiter):
    rate, clock = limiter
    rate.acquire("eutils")
    with pytest.raises(RateLimited):
        rate.acquire("eutils")
    clock[0] += 0.351
    rate.acquire("eutils")
    with pytest.raises(ValueError):
        rate.acquire("eutils", "RID123")
    with pytest.raises(ValueError):
        rate.acquire("blast", "bad!")
