"""Shared DynamoDB rate limiter for NCBI's E-utilities and BLAST APIs.

Each call reserves its global and (for BLAST polls) per-RID slots atomically.
This helper must be shared by every NCBI caller in the deployment. Callers must
not make the network request when reservation fails.
"""
from __future__ import annotations

import re
import time
from typing import Callable

from botocore.exceptions import ClientError

_RID = re.compile(r"^[A-Za-z0-9-]{5,40}$")


class RateLimited(RuntimeError):
    """The caller should retry later, through this limiter."""


class NCBIRateLimiter:
    def __init__(self, client, table_name: str, *, clock: Callable[[], float] = time.time):
        if not table_name:
            raise ValueError("rate limiter table is required")
        self._client = client
        self._table = table_name
        self._clock = clock

    def acquire(self, service: str, rid: str | None = None) -> None:
        if service == "eutils" and rid is None:
            slots = [("eutils", 350)]  # <= 3 requests/second across the deployment
        elif service == "blast" and (rid is None or isinstance(rid, str) and _RID.fullmatch(rid)):
            slots = [("blast", 10_000)]
            if rid is not None:
                slots.append((f"blast-rid#{rid}", 60_000))
        else:
            raise ValueError("invalid rate limit request")
        now = int(self._clock() * 1000)
        transaction = []
        for key, interval in slots:
            transaction.append({"Update": {
                "TableName": self._table,
                "Key": {"pk": {"S": key}},
                "UpdateExpression": "SET next_allowed = :next, expires_at = :ttl",
                "ConditionExpression": "attribute_not_exists(next_allowed) OR next_allowed <= :now",
                "ExpressionAttributeValues": {
                    ":next": {"N": str(now + interval)}, ":now": {"N": str(now)},
                    ":ttl": {"N": str(now // 1000 + 86400)},
                },
            }})
        try:
            self._client.transact_write_items(TransactItems=transaction)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "TransactionCanceledException":
                reasons = exc.response.get("CancellationReasons") or []
                if any(reason.get("Code") == "ConditionalCheckFailed" for reason in reasons):
                    raise RateLimited("NCBI request rate exceeded") from exc
            raise
