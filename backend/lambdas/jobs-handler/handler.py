"""Polling Lambda: owner-scoped job listing, status and cancellation (api.md Jobs, B25, B98).

There is no POST /jobs; runtimes create jobs. Cancellation sets the cooperative
flag, asks AgentCore to stop the runtime session with the caller's own bearer
token (the runtimes accept only SPA user tokens), then conditionally finalizes
the job as cancelled so a late completion from the runtime cannot win.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

import boto3

from http_errors import (ApiError, api_handler, decode_cursor, get_logger, invalid_request, json_response,
                         not_found, page_envelope, parse_limit, require_age_verified, user_id_from_claims)
from shared.persistence.active_runs import ActiveRunRepository
from shared.persistence.jobs import (CANCELLED, TERMINAL_STATUSES, JobRepository, JobTransitionError, public_job,
                                     valid_job_id)

logger = get_logger(__name__)
_jobs: JobRepository | None = None
_runs: ActiveRunRepository | None = None
STOP_TIMEOUT_SECONDS = 5


def _repositories() -> tuple[JobRepository, ActiveRunRepository]:
    global _jobs, _runs
    if _jobs is None:
        _jobs = JobRepository(os.environ["CEI_JOB_TABLE"])
    if _runs is None:
        _runs = ActiveRunRepository(boto3.resource("dynamodb").Table(os.environ["CEI_ACTIVE_RUNS_TABLE"]))
    return _jobs, _runs


def _bearer(event: dict[str, Any]) -> str | None:
    headers = event.get("headers") or {}
    value = next((v for k, v in headers.items() if k.lower() == "authorization"), None)
    if isinstance(value, str) and value.startswith("Bearer ") and len(value) > 7:
        return value
    return None


def _stop_runtime_session(agent_type: str, runtime_session_id: str, authorization: str | None) -> bool:
    """Best effort: the conditional cancel below is what guarantees the outcome."""
    urls = json.loads(os.environ.get("CEI_RUNTIME_STOP_URLS", "{}"))
    url = urls.get(agent_type)
    if not url or not authorization or not runtime_session_id:
        return False
    request = urllib.request.Request(url, data=b"{}", method="POST", headers={
        "Authorization": authorization, "Content-Type": "application/json",
        "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": runtime_session_id})
    try:
        with urllib.request.urlopen(request, timeout=STOP_TIMEOUT_SECONDS) as response:  # noqa: S310 (fixed https URL)
            return 200 <= response.status < 300
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # Never log the response body or the token; the type and status are enough to diagnose.
        logger.warning("runtime_stop_failed", extra={"code": getattr(exc, "code", type(exc).__name__)})
        return False


def _owned_job(jobs: JobRepository, user_id: str, job_id: Any) -> dict[str, Any]:
    if not valid_job_id(job_id):
        raise invalid_request("The jobId path parameter is not valid.")
    item = jobs.get(user_id, job_id)
    if item is None:
        raise not_found("The job was not found.")
    return item


@api_handler(logger)
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    user_id = user_id_from_claims(event)
    route = (event.get("httpMethod"), event.get("resource"))
    params = event.get("pathParameters") or {}
    jobs, runs = _repositories()

    if route == ("GET", "/jobs"):
        query = event.get("queryStringParameters") or {}
        cursor = decode_cursor(query.get("cursor"), {"user_id": lambda value: value == user_id,
                                                     "job_id": valid_job_id})
        page = jobs.list_for_user(user_id, limit=parse_limit(query), start_key=cursor)
        return json_response(200, page_envelope([public_job(item) for item in page.items], page.next_key), event)

    if route == ("GET", "/jobs/{jobId}"):
        response = json_response(200, public_job(_owned_job(jobs, user_id, params.get("jobId"))), event)
        response["headers"]["Cache-Control"] = "no-store"
        return response

    if route == ("POST", "/jobs/{jobId}/cancel"):
        # B112: a cancel writes the job's status; it requires a completed, age-verified profile.
        require_age_verified(event)
        item = _owned_job(jobs, user_id, params.get("jobId"))
        if item["status"] in TERMINAL_STATUSES:
            # Idempotent: cancelling a finished job changes nothing and reports its real state.
            return json_response(200, public_job(item), event)
        try:
            item = jobs.request_cancel(user_id, item["job_id"])
        except JobTransitionError:
            return json_response(200, public_job(jobs.get(user_id, item["job_id"]) or item), event)
        _stop_runtime_session(item["agent_type"], item.get("runtime_session_id", ""), _bearer(event))
        try:
            jobs.finish(user_id, item["job_id"], CANCELLED, error="Cancelled by the user.")
        except JobTransitionError:
            pass  # the runtime finished first; its terminal state stands
        runs.release_background(user_id, item["job_id"])
        return json_response(202, {"job_id": item["job_id"], "status": "cancelling"}, event)

    raise ApiError(404, "not_found", "The route was not found.")
