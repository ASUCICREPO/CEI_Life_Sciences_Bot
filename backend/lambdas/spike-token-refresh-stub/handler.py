"""S2 token-refresh stub (B67), reached through an IAM-authenticated Lambda Function URL.

FIXME: remove after S2 validation (B92)

Takes {"token": "<old access token>", "run_id": "<id>"} and returns a fresh client-credentials
token with the same scopes. The real endpoint (F4) checks the Job ID Store / active-run record;
this stub treats run_id "inactive" as a finished run. Runs as the SpikeRunnerRole, the only
identity allowed to read the M2M client secret. Stdlib plus boto3 (bundled in the runtime).
Never logs tokens or the client secret.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_secrets = boto3.client("secretsmanager")


def _response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }


def _scopes_of(token: str) -> str:
    # Stub only: reads the scope claim WITHOUT verifying the signature. The real endpoint
    # must take the scopes from the run record, not from a caller-supplied token.
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload)).get("scope", "")


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        body = json.loads(raw)
        token = body["token"]
        run_id = body.get("run_id", "")
        scope = _scopes_of(token)
    except (ValueError, KeyError, IndexError, TypeError):
        logger.info(json.dumps({"msg": "bad_request"}))
        return _response(400, {"error": "bad_request"})

    if run_id == "inactive":
        logger.info(json.dumps({"msg": "run_not_active", "run_id": run_id}))
        return _response(409, {"error": "run_not_active"})

    client_secret = _secrets.get_secret_value(SecretId=os.environ["SECRET_ARN"])["SecretString"]
    client_id = os.environ["CLIENT_ID"]
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    request = urllib.request.Request(
        os.environ["TOKEN_ENDPOINT"],
        data=urllib.parse.urlencode({"grant_type": "client_credentials", "scope": scope}).encode(),
        headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:  # noqa: S310 - fixed https endpoint
            fresh = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        logger.error(json.dumps({"msg": "token_endpoint_error", "status": exc.code}))
        return _response(502, {"error": "token_endpoint_error", "status": exc.code})

    logger.info(json.dumps({"msg": "refreshed", "run_id": run_id, "scope": scope}))
    return _response(
        200,
        {
            "access_token": fresh["access_token"],
            "token_type": fresh.get("token_type", "Bearer"),
            "expires_in": fresh.get("expires_in"),
            "scope": scope,
        },
    )
