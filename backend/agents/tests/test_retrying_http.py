from __future__ import annotations

import pytest
import requests

from shared.tools import retrying_http


class FakeResponse:
    def __init__(self, status_code: int, headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self.headers = headers or {}


@pytest.fixture
def calls(monkeypatch):
    """Queue of outcomes for requests.request: a FakeResponse or an exception to raise."""
    queue: list[object] = []
    seen: list[tuple[str, str, dict]] = []

    def fake_request(method, url, **kwargs):
        seen.append((method, url, kwargs))
        outcome = queue.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(retrying_http.requests, "request", fake_request)
    monkeypatch.setattr(retrying_http.random, "uniform", lambda lo, hi: hi)
    return queue, seen


def test_returns_first_success_without_sleeping(calls):
    queue, seen = calls
    queue.append(FakeResponse(200))
    sleeps: list[float] = []
    resp = retrying_http.get("https://api.example.org/x", params={"q": "a"}, timeout=5, sleep=sleeps.append)
    assert resp.status_code == 200
    assert sleeps == []
    assert seen == [("GET", "https://api.example.org/x", {"params": {"q": "a"}, "timeout": 5})]


def test_reserves_each_retry_and_stops_when_reservation_fails(calls):
    queue, seen = calls
    queue.extend([FakeResponse(503), FakeResponse(200)])
    reservations = []

    def reserve():
        reservations.append(True)
        if len(reservations) == 2:
            raise RuntimeError("reservation unavailable")

    with pytest.raises(RuntimeError, match="reservation unavailable"):
        retrying_http.get("https://api.example.org/x", before_request=reserve, sleep=lambda _: None)
    assert len(reservations) == 2
    assert len(seen) == 1


def test_retries_5xx_then_succeeds_with_exponential_backoff(calls):
    queue, seen = calls
    queue.extend([FakeResponse(503), FakeResponse(502), FakeResponse(200)])
    sleeps: list[float] = []
    resp = retrying_http.get("https://api.example.org/x", sleep=sleeps.append)
    assert resp.status_code == 200
    assert len(seen) == 3
    assert sleeps == [0.5, 1.0]  # uniform(0, 0.5 * 2^(attempt-1)) pinned to its upper bound


def test_honors_numeric_retry_after_capped(calls):
    queue, _ = calls
    queue.extend([FakeResponse(429, {"Retry-After": "2"}), FakeResponse(429, {"Retry-After": "120"}), FakeResponse(200)])
    sleeps: list[float] = []
    assert retrying_http.get("https://api.example.org/x", sleep=sleeps.append).status_code == 200
    assert sleeps == [2.0, retrying_http.MAX_SLEEP_S]


def test_http_date_retry_after_falls_back_to_backoff(calls):
    queue, _ = calls
    queue.extend([FakeResponse(503, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}), FakeResponse(200)])
    sleeps: list[float] = []
    retrying_http.get("https://api.example.org/x", sleep=sleeps.append)
    assert sleeps == [0.5]


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_does_not_retry_client_errors(calls, status):
    queue, seen = calls
    queue.append(FakeResponse(status))
    assert retrying_http.get("https://api.example.org/x", sleep=lambda s: None).status_code == status
    assert len(seen) == 1


def test_returns_last_retryable_response_when_attempts_run_out(calls):
    queue, seen = calls
    queue.extend([FakeResponse(503), FakeResponse(503), FakeResponse(503)])
    assert retrying_http.get("https://api.example.org/x", sleep=lambda s: None).status_code == 503
    assert len(seen) == retrying_http.DEFAULT_ATTEMPTS


def test_retries_network_errors_then_succeeds(calls):
    queue, _ = calls
    queue.extend([requests.ConnectionError("reset"), requests.Timeout("slow"), FakeResponse(200)])
    assert retrying_http.post("https://api.example.org/graphql", json={}, sleep=lambda s: None).status_code == 200


def test_reraises_network_error_after_last_attempt(calls):
    queue, _ = calls
    queue.extend([requests.ConnectionError("a"), requests.ConnectionError("b"), requests.ConnectionError("c")])
    with pytest.raises(requests.ConnectionError, match="c"):
        retrying_http.get("https://api.example.org/x", sleep=lambda s: None)


def test_attempts_must_be_positive():
    with pytest.raises(ValueError):
        retrying_http.get("https://api.example.org/x", attempts=0)


def test_retry_log_has_host_only_never_the_query(calls, caplog):
    queue, _ = calls
    queue.extend([FakeResponse(503), FakeResponse(200)])
    caplog.set_level("WARNING")
    retrying_http.get("https://eutils.example.gov/esearch?api_key=SECRETKEY", sleep=lambda s: None)
    assert "eutils.example.gov" in caplog.text
    assert "SECRETKEY" not in caplog.text
