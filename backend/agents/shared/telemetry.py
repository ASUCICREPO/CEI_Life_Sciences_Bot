"""Initialize optional AgentCore instrumentation before application imports."""
from __future__ import annotations

import logging
import os


def configure_telemetry() -> None:
    """Start ADOT in deployed runtimes; a telemetry outage must not prevent serving requests."""
    if os.environ.get("CEI_TELEMETRY_ENABLED") != "true":
        return
    try:
        # Equivalent to the OpenTelemetry Python sitecustomize initialization. Starting it
        # in Python avoids uv's host-specific console script in cross-platform CodeZip builds.
        from opentelemetry.instrumentation.auto_instrumentation import initialize

        initialize(swallow_exceptions=False)
    except Exception as exc:  # noqa: BLE001 - observability must not take the agent offline
        logging.getLogger(__name__).error("telemetry_initialization_failed error_type=%s", type(exc).__name__)
