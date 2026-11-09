"""Unwired adapters for the Research Router's authenticated service calls.

The consumer must drain the AgentCore response before returning; until the runtime
emits real CEI UI events this is not a streaming implementation.
"""
from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

import requests


class RouterServiceError(RuntimeError):
    """Redacted service failure; never contains tokens, secrets or request bodies."""


def _endpoint(url: str, host_pattern: str, path_pattern: str) -> None:
    try:
        parts = urlsplit(url)
        valid = (parts.scheme == "https" and parts.username is None and parts.password is None
                 and parts.port in (None, 443) and not parts.fragment
                 and re.fullmatch(host_pattern, parts.hostname or "")
                 and re.fullmatch(path_pattern, parts.path))
    except ValueError:
        valid = False
    if not valid:
        raise RouterServiceError("The service configuration is invalid.")


def session_owned(table: Any, user_id: str, session_id: str) -> bool:
    """Strongly consistent ownership lookup against the authenticated user's key."""
    result = table.get_item(Key={"user_id": user_id, "session_id": session_id}, ConsistentRead=True)
    return isinstance(result.get("Item"), dict)


class CognitoTokenIssuer:
    def __init__(self, *, endpoint: str, client_id: str, secret_arn: str, secrets_client: Any) -> None:
        _endpoint(endpoint, r"[a-z0-9-]+\.auth\.[a-z0-9-]+\.amazoncognito\.com(?:\.cn)?", r"/oauth2/token")
        if urlsplit(endpoint).query or not client_id or not secret_arn:
            raise RouterServiceError("The token service configuration is invalid.")
        self.endpoint, self.client_id = endpoint, client_id
        self.secret_arn, self.secrets_client = secret_arn, secrets_client

    async def __call__(self, user_id: str, session_id: str, scopes: tuple[str, ...]) -> Mapping[str, Any]:
        def request() -> Mapping[str, Any]:
            try:
                secret = self.secrets_client.get_secret_value(SecretId=self.secret_arn)["SecretString"]
                response = requests.post(
                    self.endpoint, auth=(self.client_id, secret),
                    data={"grant_type": "client_credentials", "scope": " ".join(scopes),
                          "aws_client_metadata": json.dumps({"cei_user_sub": user_id, "cei_session_id": session_id})},
                    timeout=10, allow_redirects=False,
                )
                if response.status_code != 200:
                    raise RouterServiceError("The token service is unavailable.")
                body = response.json()
                if not isinstance(body, dict):
                    raise RouterServiceError("The token service returned an invalid response.")
                return body
            except RouterServiceError:
                raise
            except Exception:
                raise RouterServiceError("The token service is unavailable.") from None
        return await asyncio.to_thread(request)


class RuntimeInvoker:
    def __init__(self, *, url: str, consume: Callable[[requests.Response], Awaitable[Any]]) -> None:
        _endpoint(url, r"bedrock-agentcore\.[a-z0-9-]+\.amazonaws\.com(?:\.cn)?", r"/runtimes/[^/]+/invocations")
        if urlsplit(url).query not in ("", "qualifier=DEFAULT"):
            raise RouterServiceError("The agent service configuration is invalid.")
        self.url, self.consume = url, consume

    async def __call__(self, bearer: str, gateway_token: str, session_id: str, payload: Mapping[str, Any]) -> Any:
        def send() -> requests.Response:
            try:
                return requests.post(
                    self.url,
                    headers={"Authorization": f"Bearer {bearer}", "X-Cei-Gateway-Token": gateway_token,
                             "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id,
                             "Accept": "text/event-stream, application/json"},
                    json=payload, stream=True, timeout=(10, 900), allow_redirects=False,
                )
            except requests.RequestException:
                raise RouterServiceError("The agent service is unavailable.") from None
        response = await asyncio.to_thread(send)
        try:
            if response.status_code != 200:
                raise RouterServiceError("The agent service rejected the request.")
            return await self.consume(response)
        finally:
            response.close()
