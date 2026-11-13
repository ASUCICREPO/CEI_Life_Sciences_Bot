"""Bounded browser failure counters; never accept or log user content."""
from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
from typing import Any

log = logging.getLogger()
log.setLevel(logging.INFO)
SOURCES = frozenset({"agent", "sessions", "stream", "window", "auth"})
FAILURES = frozenset({"network", "timeout", "http_error", "invalid_response", "stream_dropped", "unhandled_exception", "render_error", "auth_error"})
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
MAX_BODY = 1024


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    headers = event.get("headers") or {}
    origin = next((v for k, v in headers.items() if k.lower() == "origin"), None)
    allowed = os.environ.get("ALLOWED_ORIGINS", "").split(",")
    cors = {"Access-Control-Allow-Origin": origin, "Vary": "Origin"} if origin in allowed and origin else {}

    def response(status: int) -> dict[str, Any]:
        return {"statusCode": status, "headers": cors, "body": ""}

    claims = ((event.get("requestContext") or {}).get("authorizer") or {}).get("claims") or {}
    if not isinstance(claims.get("sub"), str) or not claims["sub"]:
        return response(401)
    try:
        raw = event.get("body")
        if not isinstance(raw, str) or len(raw) > MAX_BODY * 2:
            return response(400)
        data = base64.b64decode(raw, validate=True) if event.get("isBase64Encoded") else raw.encode("utf-8")
        if len(data) > MAX_BODY:
            return response(400)
        payload = json.loads(data)
    except (ValueError, UnicodeError, binascii.Error):
        return response(400)
    if not isinstance(payload, dict) or set(payload) - {"source", "failure", "status", "request_id"}:
        return response(400)
    if not isinstance(payload.get("source"), str) or payload["source"] not in SOURCES:
        return response(400)
    if not isinstance(payload.get("failure"), str) or payload["failure"] not in FAILURES:
        return response(400)
    status = payload.get("status")
    if status is not None and (type(status) is not int or not 400 <= status <= 599):
        return response(400)
    linked = payload.get("request_id")
    if linked is not None and (not isinstance(linked, str) or not UUID.fullmatch(linked)):
        return response(400)
    log.info(json.dumps({"event": "browser_failure", "source": payload["source"], "failure": payload["failure"],
                         "status": status, "linked_request_id": linked,
                         "lambda_request_id": getattr(context, "aws_request_id", "unknown")}))
    return response(204)
