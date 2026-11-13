import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws
from unittest.mock import Mock

from limiter import NCBIRateLimiter, RateLimited


def test_transaction_conflict_is_retryable_and_other_failures_propagate():
    client = Mock()
    client.transact_write_items.side_effect = ClientError({"Error": {"Code": "TransactionCanceledException"},
        "CancellationReasons": [{"Code": "TransactionConflict"}]}, "TransactWriteItems")
    with pytest.raises(RateLimited):
        NCBIRateLimiter(client, "limits").acquire("eutils")
    client.transact_write_items.side_effect = ClientError({"Error": {"Code": "AccessDeniedException"}}, "TransactWriteItems")
    with pytest.raises(ClientError):
        NCBIRateLimiter(client, "limits").acquire("eutils")


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


def test_new_blast_request_cannot_be_polled_for_one_minute(limiter):
    rate, clock = limiter
    rate.acquire("blast")
    rate.register_blast("RID123")
    clock[0] += 10
    with pytest.raises(RateLimited):
        rate.acquire("blast", "RID123")
    rate.register_blast("RID123")  # Retry cannot shorten or consume the slot.
    clock[0] += 50
    rate.acquire("blast", "RID123")
