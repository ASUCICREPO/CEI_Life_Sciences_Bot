"""Builds the BedrockAgentCoreApp for every agent entrypoint, with browser CORS when origins are configured.

The deployed SPA calls ``https://bedrock-agentcore.<region>/runtimes/<arn>/invocations`` directly, which is a
cross-origin request with ``Authorization`` + ``Content-Type: application/json`` -> the browser sends a preflight
``OPTIONS``. The SDK only routes ``POST /invocations``, ``GET /ping`` and ``/ws``, so without this middleware the
preflight gets a 405 and the real call never happens. Starlette's CORSMiddleware answers preflights before routing.
"""
from __future__ import annotations

from typing import Sequence

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware

from shared.config import config

SESSION_ID_HEADER = "x-amzn-bedrock-agentcore-runtime-session-id"
ALLOWED_METHODS = ["POST", "OPTIONS"]
ALLOWED_HEADERS = ["authorization", "content-type", "accept", SESSION_ID_HEADER]
EXPOSED_HEADERS = [SESSION_ID_HEADER]
PREFLIGHT_MAX_AGE_SECONDS = 3600


def cors_middleware(origins: Sequence[str]) -> list[Middleware]:
    if not origins:
        return []
    return [
        Middleware(
            CORSMiddleware,
            allow_origins=list(origins),
            allow_methods=ALLOWED_METHODS,
            allow_headers=ALLOWED_HEADERS,
            expose_headers=EXPOSED_HEADERS,
            allow_credentials=False,  # Bearer token in a header, no cookies
            max_age=PREFLIGHT_MAX_AGE_SECONDS,
        )
    ]


def create_app(origins: Sequence[str] | None = None) -> BedrockAgentCoreApp:
    """Return the runtime app; ``origins`` defaults to ``CEI_ALLOWED_ORIGINS``. Empty list => plain app."""
    resolved = list(origins) if origins is not None else config.allowed_origins_list
    middleware = cors_middleware(resolved)
    return BedrockAgentCoreApp(middleware=middleware or None)
