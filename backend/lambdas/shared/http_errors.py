"""Shared REST helpers: error envelope, cursor pagination, CORS, JSON logging (api.md).

Shipped to every REST Lambda in the python-deps layer (top-level module `http_errors`).
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
from decimal import Decimal
from functools import wraps
from typing import Any, Callable

DEFAULT_LIMIT = 20  # B78
MAX_LIMIT = 100  # B78
# The only text a caller sees for a server-side failure; the real cause goes to the function's CloudWatch log.
GENERIC_INTERNAL_ERROR = "It's not you, it's us. Something went wrong"

ERROR_CODES = frozenset(
    {
        "invalid_request",
        "unauthorized",
        "forbidden",
        "not_found",
        "rate_limited",
        "concurrency_limit",
        "stale_revision",
        "service_unavailable",
        "internal_error",
    }
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("request_id", "route"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        logger.propagate = False
    logger.setLevel(logging.INFO)
    return logger


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, retriable: bool = False) -> None:
        super().__init__(message)
        assert code in ERROR_CODES, code
        self.status = status
        self.code = code
        self.message = message
        self.retriable = retriable


def invalid_request(message: str) -> ApiError:
    return ApiError(400, "invalid_request", message)


def not_found(message: str = "The requested resource was not found.") -> ApiError:
    return ApiError(404, "not_found", message)


def request_id(event: dict[str, Any], context: Any = None) -> str:
    rid = (event.get("requestContext") or {}).get("requestId")
    return rid or getattr(context, "aws_request_id", None) or "req_unknown"


def _allowed_origins() -> list[str]:
    return [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]


def cors_headers(event: dict[str, Any]) -> dict[str, str]:
    """Echo the request Origin only when it is on the allowlist (never `*`)."""
    headers = event.get("headers") or {}
    origin = next((v for k, v in headers.items() if k.lower() == "origin"), None)
    if origin and origin in _allowed_origins():
        return {"Access-Control-Allow-Origin": origin, "Vary": "Origin"}
    return {}


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def json_response(status: int, body: Any, event: dict[str, Any] | None = None) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    headers.update(cors_headers(event or {}))
    return {"statusCode": status, "headers": headers, "body": json.dumps(body, default=_json_default)}


def error_body(code: str, message: str, rid: str, retriable: bool = False) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "request_id": rid, "retriable": retriable}}


def error_response(
    status: int,
    code: str,
    message: str,
    rid: str,
    retriable: bool = False,
    event: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return json_response(status, error_body(code, message, rid, retriable), event)


# --- pagination -------------------------------------------------------------


def parse_limit(params: dict[str, str] | None, default: int = DEFAULT_LIMIT) -> int:
    raw = (params or {}).get("limit")
    if raw is None or raw == "":
        return default
    try:
        limit = int(raw)
    except ValueError:
        raise invalid_request("The limit parameter must be an integer.") from None
    if not 1 <= limit <= MAX_LIMIT:
        raise invalid_request(f"The limit parameter must be between 1 and {MAX_LIMIT}.")
    return limit


def encode_cursor(key: dict[str, Any] | None) -> str | None:
    if not key:
        return None
    raw = json.dumps(key, default=_json_default, sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(
    cursor: str | None, expected: dict[str, Callable[[Any], bool]]
) -> dict[str, Any] | None:
    """Decode an opaque cursor and reject anything that is not exactly the expected key shape.

    `expected` maps each key attribute to a predicate; the handler passes predicates that pin
    the partition key to the caller (user or session), so a cursor from another partition fails.
    """
    if not cursor:
        return None
    try:
        key = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise invalid_request("The cursor is not valid.") from None
    if not isinstance(key, dict) or set(key) != set(expected):
        raise invalid_request("The cursor is not valid.")
    for attr, check in expected.items():
        if not check(key[attr]):
            raise invalid_request("The cursor is not valid.")
    return key


def page_envelope(items: list[Any], next_key: dict[str, Any] | None) -> dict[str, Any]:
    next_cursor = encode_cursor(next_key)
    return {"items": items, "next_cursor": next_cursor, "has_more": next_cursor is not None}


# --- request helpers ---------------------------------------------------------


def user_id_from_claims(event: dict[str, Any]) -> str:
    """B12: the caller identity comes only from the Cognito authorizer claims."""
    claims = ((event.get("requestContext") or {}).get("authorizer") or {}).get("claims") or {}
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub:
        raise ApiError(401, "unauthorized", "Authentication is required.")
    return sub


def parse_json_body(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("body")
    if raw is None or raw == "":
        raise invalid_request("A JSON request body is required.")
    if event.get("isBase64Encoded"):
        try:
            raw = base64.b64decode(raw).decode()
        except (binascii.Error, UnicodeDecodeError):
            raise invalid_request("The request body is not valid.") from None
    try:
        body = json.loads(raw)
    except ValueError:
        raise invalid_request("The request body must be valid JSON.") from None
    if not isinstance(body, dict):
        raise invalid_request("The request body must be a JSON object.")
    return body


def api_handler(logger: logging.Logger) -> Callable[[Callable[..., dict[str, Any]]], Callable[..., dict[str, Any]]]:
    """Wrap a route function so ApiError and unexpected errors become envelope responses."""

    def decorator(fn: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
        @wraps(fn)
        def wrapper(event: dict[str, Any], context: Any) -> dict[str, Any]:
            rid = request_id(event, context)
            try:
                return fn(event, context)
            except ApiError as exc:
                return error_response(exc.status, exc.code, exc.message, rid, exc.retriable, event)
            except Exception:
                # full traceback (incl. chained causes) to CloudWatch; the caller gets the generic text only
                logger.exception("unhandled error", extra={"request_id": rid})
                return error_response(500, "internal_error", GENERIC_INTERNAL_ERROR, rid, False, event)

        return wrapper

    return decorator
