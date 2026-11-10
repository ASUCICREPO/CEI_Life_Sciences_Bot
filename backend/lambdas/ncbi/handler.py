"""Gateway target for read-only taxonomy and sequence lookups.

BLAST is intentionally unavailable here until per-user request ownership is
bound to a trusted identity and persisted alongside the submitted RID.
"""
from __future__ import annotations

import logging
import os
import random
import time
from functools import lru_cache
from typing import Any

import boto3
from limiter import NCBIRateLimiter, RateLimited
from targets import sequence_efetch, taxonomy_lineage

log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def limiter() -> NCBIRateLimiter:
    return NCBIRateLimiter(boto3.client("dynamodb"), os.environ["NCBI_RATE_LIMIT_TABLE"])


def acquire(service: str, rid: str | None = None) -> None:
    # A taxonomy name lookup needs two E-utilities requests in succession.
    # Wait briefly for the shared slot while keeping every request rate-limited.
    for attempt in range(10):
        try:
            limiter().acquire(service, rid)
            return
        except RateLimited:
            if attempt == 9:
                raise
            time.sleep(random.uniform(0.36, 0.46))


def handler(event: Any, context: Any) -> dict[str, Any]:
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    name = custom.get("bedrockAgentCoreToolName", "")
    if not isinstance(name, str) or "___" not in name or not isinstance(event, dict):
        return {"error": {"code": "invalid_request", "message": "Invalid tool request."}}
    operation = name.split("___", 1)[1]
    try:
        if operation == "taxonomy_lineage" and set(event) == {"taxon"}:
            return {"result": taxonomy_lineage(event["taxon"], acquire=acquire)}
        if operation == "sequence_efetch" and set(event) == {"accession", "database", "format"}:
            return {"result": sequence_efetch(event["accession"], database=event["database"],
                                              format=event["format"], acquire=acquire)}
        return {"error": {"code": "invalid_request", "message": "Unknown tool or invalid arguments."}}
    except RateLimited:
        return {"error": {"code": "rate_limited", "message": "NCBI is busy. Retry later.", "retriable": True}}
    except (ValueError, TypeError, KeyError):
        return {"error": {"code": "invalid_input", "message": "The NCBI request could not be processed."}}
    except Exception as exc:  # no upstream payloads or input text in logs
        log.warning("ncbi_request_failed error_type=%s", type(exc).__name__)
        return {"error": {"code": "service_unavailable", "message": "NCBI is temporarily unavailable.", "retriable": True}}
