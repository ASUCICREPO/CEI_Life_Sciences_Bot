"""Retrying HTTP calls for the data-source tools (tools.md section 1 gap, B22).

Every data-source tool makes its upstream calls through `get`/`post` here, so the same retry
policy runs in local CLI mode and inside the F4 Gateway Lambdas.

Retried: connection errors, timeouts, HTTP 429 and 5xx (500/502/503/504).
Not retried: every other status (for example a 404 "no match", which PubChem uses for empty results).
Backoff: exponential with full jitter, or the upstream's `Retry-After` seconds when it sends one,
each sleep capped at MAX_SLEEP_S.

Upstream rate limits are not encoded here. Only NCBI's (3 req/s, 10 with a key) is documented
(tools.md section 1); the other nine providers' limits are still TBD, so this module reacts to
429/Retry-After instead of pacing to a guessed number.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Callable
from urllib.parse import urlsplit

import requests

logger = logging.getLogger(__name__)

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
DEFAULT_ATTEMPTS = 3
BASE_DELAY_S = 0.5
# ADR: 4 s cap per sleep | Rationale: a source Lambda may make two upstream calls (PubMed, PubChem); 3 attempts x 10 s timeout + 2 capped sleeps keeps one call under ~40 s and a two-call tool under the Lambda timeout | Alternative: honoring any Retry-After rejected — an upstream asking for 60 s would exhaust the Lambda
MAX_SLEEP_S = 4.0


def _retry_after_seconds(response: requests.Response) -> float | None:
    """Seconds from a numeric Retry-After header; None when absent or an HTTP-date (not parsed)."""
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        seconds = float(raw.strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def _backoff_seconds(attempt: int) -> float:
    """Full-jitter exponential backoff: uniform(0, BASE * 2^(attempt-1))."""
    return random.uniform(0, BASE_DELAY_S * (2 ** (attempt - 1)))


def request(
    method: str,
    url: str,
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
    **kwargs: Any,
) -> requests.Response:
    """`requests.request` with retry. Returns the last response; re-raises the last network error.

    Callers keep their own `raise_for_status()` / status handling, exactly as with plain `requests`.
    """
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    host = urlsplit(url).netloc  # log the host only: query strings can carry a user's API key
    for attempt in range(1, attempts + 1):
        try:
            response = requests.request(method, url, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as exc:
            if attempt == attempts:
                raise
            delay = _backoff_seconds(attempt)
            reason = type(exc).__name__
        else:
            if response.status_code not in RETRY_STATUSES or attempt == attempts:
                return response
            retry_after = _retry_after_seconds(response)
            delay = retry_after if retry_after is not None else _backoff_seconds(attempt)
            reason = f"HTTP {response.status_code}"
        delay = min(delay, MAX_SLEEP_S)
        logger.warning(
            {"event": "upstream_retry", "host": host, "attempt": attempt, "reason": reason, "sleep_s": round(delay, 2)}
        )
        sleep(delay)
    raise AssertionError("unreachable")  # the loop always returns or raises


def get(url: str, **kwargs: Any) -> requests.Response:
    return request("GET", url, **kwargs)


def post(url: str, **kwargs: Any) -> requests.Response:
    return request("POST", url, **kwargs)
