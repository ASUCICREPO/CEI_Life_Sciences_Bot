"""Agent invocation proxy behind a Lambda function URL.

The SPA cannot call the AgentCore data plane directly: AgentCore documents no CORS support, and a browser
preflight never carries the Authorization header its JWT authorizer requires. The browser instead POSTs to
this function URL, whose CORS (preflight + response headers) is configured on the URL itself and handled
by the Lambda service. This handler forwards the request to the agent's AgentCore invocation URL with the
caller's own bearer token, so AgentCore's Cognito authorizer (layer 1) and identity.py (layer 2) still
authenticate every request. The proxy holds no AWS permission on the runtimes and never logs tokens,
session ids, or bodies.

Stdlib only; the function URL is configured with BUFFERED invoke mode and a 15-minute timeout.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import socket
import time
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"
# AgentCore accepts 33-256 characters; the SPA sends a UUID (36). Charset matches the agents' contract.
_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{33,256}$")
_AGENT_PATH_PATTERN = re.compile(r"^/([a-z]+)/?$")
_MAX_TOKEN_LENGTH = 8192
_MAX_BODY_BYTES = 1_000_000
# Upstream read timeout stays under the Lambda timeout (900 s) so the proxy can still answer with a 504.
_DEFAULT_UPSTREAM_TIMEOUT_S = 870.0


def _agent_urls() -> dict[str, str]:
    try:
        urls = json.loads(os.environ.get("AGENT_URLS", "{}"))
    except ValueError:
        return {}
    if not isinstance(urls, dict):
        return {}
    return {k: v for k, v in urls.items() if isinstance(k, str) and isinstance(v, str) and v.startswith("https://")}


def _upstream_timeout() -> float:
    try:
        return float(os.environ.get("UPSTREAM_TIMEOUT_SECONDS", _DEFAULT_UPSTREAM_TIMEOUT_S))
    except ValueError:
        return _DEFAULT_UPSTREAM_TIMEOUT_S


def _request_id(context: Any) -> str:
    return getattr(context, "aws_request_id", None) or "unknown"


def _error(status: int, code: str, message: str, rid: str, retriable: bool = False) -> dict[str, Any]:
    # No CORS headers here: the function URL adds them, and duplicates would make the browser reject the response.
    body = {"error": {"code": code, "message": message, "request_id": rid, "retriable": retriable}}
    return {"statusCode": status, "headers": {"content-type": "application/json"}, "body": json.dumps(body)}


def _log(agent: str | None, status: int, started: float, rid: str) -> None:
    logger.info(
        json.dumps(
            {
                "event": "agent_proxy",
                "agent": agent,
                "status": status,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "request_id": rid,
            }
        )
    )


def _read_body(event: dict[str, Any]) -> bytes | None:
    raw = event.get("body")
    if raw is None or raw == "":
        return None
    if event.get("isBase64Encoded"):
        try:
            return base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            return None
    return raw.encode("utf-8")


def _response(status: int, content_type: str | None, payload: bytes) -> dict[str, Any]:
    headers = {"content-type": content_type or "application/json"}
    try:
        return {"statusCode": status, "headers": headers, "body": payload.decode("utf-8")}
    except UnicodeDecodeError:
        return {
            "statusCode": status,
            "headers": headers,
            "body": base64.b64encode(payload).decode("ascii"),
            "isBase64Encoded": True,
        }


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    started = time.monotonic()
    rid = _request_id(context)
    http = (event.get("requestContext") or {}).get("http") or {}
    method = str(http.get("method", "")).upper()

    if method == "OPTIONS":
        # The function URL answers preflights itself; this only covers a preflight that still reaches the code.
        return {"statusCode": 204, "headers": {}, "body": ""}
    if method != "POST":
        return _error(405, "invalid_request", "Only POST is supported.", rid)

    match = _AGENT_PATH_PATTERN.match(event.get("rawPath") or "")
    agent = match.group(1) if match else None
    target = _agent_urls().get(agent or "")
    if target is None:
        _log(agent, 404, started, rid)
        return _error(404, "not_found", "Unknown agent.", rid)

    # Function URL events carry lower-cased header names.
    headers = {str(k).lower(): v for k, v in (event.get("headers") or {}).items()}
    authorization = headers.get("authorization") or ""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token or len(token) > _MAX_TOKEN_LENGTH or not token.isascii():
        _log(agent, 401, started, rid)
        return _error(401, "unauthorized", "Authentication is required.", rid)

    session_id = headers.get(SESSION_HEADER.lower()) or ""
    if not _SESSION_ID_PATTERN.match(session_id):
        _log(agent, 400, started, rid)
        return _error(400, "invalid_request", "A valid session id header is required.", rid)

    body = _read_body(event)
    if body is None:
        _log(agent, 400, started, rid)
        return _error(400, "invalid_request", "A JSON request body is required.", rid)
    if len(body) > _MAX_BODY_BYTES:
        _log(agent, 413, started, rid)
        return _error(413, "invalid_request", "The request body is too large.", rid)

    request = urllib.request.Request(
        target,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            SESSION_HEADER: session_id,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=_upstream_timeout()) as upstream:  # noqa: S310 - https only
            result = _response(upstream.status, upstream.headers.get("content-type"), upstream.read())
    except urllib.error.HTTPError as exc:
        # AgentCore's own 4xx/5xx (401 bad token, 429 throttled, ...) pass through with their status code.
        payload = exc.read() if exc.fp is not None else b""
        if payload:
            result = _response(exc.code, exc.headers.get("content-type") if exc.headers else None, payload)
        else:
            result = _error(exc.code, "service_unavailable" if exc.code >= 500 else "invalid_request",
                            "The agent service rejected the request.", rid, exc.code in (429, 503))
    except (TimeoutError, socket.timeout):
        result = _error(504, "service_unavailable", "The agent did not respond in time.", rid, True)
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            result = _error(504, "service_unavailable", "The agent did not respond in time.", rid, True)
        else:
            result = _error(502, "service_unavailable", "The agent service could not be reached.", rid, True)
    except Exception:  # noqa: BLE001 - never leak internals to the caller
        logger.exception("agent proxy failure")
        result = _error(502, "service_unavailable", "The agent service could not be reached.", rid, True)

    _log(agent, result["statusCode"], started, rid)
    return result
