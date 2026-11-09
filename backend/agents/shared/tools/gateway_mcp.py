"""Small, fail-closed AgentCore Gateway MCP client.

The caller supplies the per-run scoped token; this module neither stores nor logs it.
The S2 spike established 2025-03-26 as the deployed Gateway protocol version.
"""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit

import requests


class GatewayCallError(RuntimeError):
    """A redacted Gateway failure safe to expose to an agent."""


class GatewayDeniedError(GatewayCallError):
    """The scoped principal cannot invoke this source."""


def call_gateway_tool(url: str, token: str, source_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        endpoint = urlsplit(url)
        host = endpoint.hostname or ""
        valid = (endpoint.scheme == "https" and not endpoint.username and not endpoint.password
                 and endpoint.port in (None, 443) and endpoint.path == "/mcp" and not endpoint.query
                 and not endpoint.fragment and re.fullmatch(
                     r"[a-z0-9-]+\.gateway\.bedrock-agentcore\.[a-z0-9-]+\.amazonaws\.com(?:\.cn)?", host))
    except ValueError:
        valid = False
    if not valid:
        raise GatewayCallError("The data source gateway configuration is invalid.")
    if not token or not re.fullmatch(r"[a-z0-9]+", source_id):
        raise GatewayCallError("The data source gateway request is invalid.")
    try:
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream",
                     "MCP-Protocol-Version": "2025-03-26"},
            json={"jsonrpc": "2.0", "id": "call", "method": "tools/call",
                  "params": {"name": f"{source_id}___search", "arguments": arguments}},
            # The slowest Gateway target has a 150-second Lambda timeout.
            timeout=160,
            allow_redirects=False,
        )
        if response.status_code in (401, 403):
            raise GatewayDeniedError("Access to this data source was denied.")
        if response.status_code != 200:
            raise GatewayCallError("The data source gateway request failed.")
        raw = response.text
        if not raw.lstrip().startswith("{"):
            frames = [line[5:].strip() for line in raw.splitlines() if line.startswith("data:")]
            raw = frames[-1] if frames else ""
        message = json.loads(raw)
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or message.get("id") != "call":
            raise GatewayCallError("The data source gateway returned an invalid response.")
        error = message.get("error")
        if isinstance(error, dict):
            if error.get("code") == -32002:
                raise GatewayDeniedError("Access to this data source was denied.")
            raise GatewayCallError("The data source gateway request failed.")
        result = message.get("result")
        if not isinstance(result, dict) or result.get("isError"):
            raise GatewayCallError("The data source gateway request failed.")
        content = result.get("content", [])
        text = next((c.get("text") for c in content if isinstance(c, dict) and c.get("type") == "text"), None)
        payload = json.loads(text) if isinstance(text, str) else result.get("structuredContent")
        if not isinstance(payload, dict):
            raise GatewayCallError("The data source gateway returned an invalid response.")
        metadata = payload.get("query_metadata")
        if isinstance(metadata, dict) and metadata.get("error"):
            raise GatewayCallError("The data source could not complete the request.")
        return payload
    except GatewayCallError:
        raise
    except (requests.RequestException, ValueError, TypeError):
        # Never include request/response objects: they can contain credentials or source data.
        raise GatewayCallError("The data source gateway request failed.") from None
