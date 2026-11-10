"""Development-only target: detect whether the Gateway forwards injected arguments.

This intentionally trusts its input and must never be exposed as an application target.
Only a restricted, isolated test deployment may use it.
"""
from typing import Any


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    identity = event.get("_cei_spike_identity") if isinstance(event, dict) else None
    return {"identity": identity, "present": isinstance(identity, dict)}
