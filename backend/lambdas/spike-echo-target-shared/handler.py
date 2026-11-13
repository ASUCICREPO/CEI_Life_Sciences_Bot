"""S2 tool-scope spike echo target "shared" (Gateway Lambda target, B70 shared tools).

FIXME: remove after S2 validation (B92)

Echoes its input plus the Gateway tool name. Stdlib only.
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TARGET = "shared"


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    # The Gateway passes the tool arguments as the event and the tool name ("<target>___<tool>")
    # in the Lambda client context.
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    tool_name = custom.get("bedrockAgentCoreToolName")
    logger.info(json.dumps({"msg": "echo", "target": TARGET, "tool": tool_name}))
    return {"target": TARGET, "tool": tool_name, "echo": event}
