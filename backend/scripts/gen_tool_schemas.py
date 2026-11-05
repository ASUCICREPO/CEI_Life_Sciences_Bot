#!/usr/bin/env python3
"""Generate the F4 Gateway tool schemas from the data-source tools' pydantic InputSchemas.

Writes backend/lambdas/data-source-tool-schemas.json, which backend/lib/constructs/tool-gateway.ts
reads to declare each Gateway target's inline tool schema. Keeping one generated file means the tool
the Gateway advertises cannot drift from what the Lambda validates (a pytest checks it is current).

The Gateway's SchemaDefinition accepts only type / properties / required / items / description,
so bounds and defaults move into the property description text.

Usage (from backend/agents):  uv run python ../scripts/gen_tool_schemas.py [--check]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "agents"))

from shared.tools.data_sources import TOOLS_BY_SOURCE_ID  # noqa: E402

OUTPUT = BACKEND / "lambdas" / "data-source-tool-schemas.json"
TOOL_NAME = "search"
_GATEWAY_TYPES = {"string", "integer", "number", "boolean", "array", "object"}


def _property(name: str, spec: dict[str, Any]) -> dict[str, Any]:
    kind = spec.get("type")
    if kind not in _GATEWAY_TYPES:
        raise ValueError(f"field {name!r}: unsupported JSON type {kind!r} for a Gateway tool schema")
    parts = [spec.get("description") or spec.get("title") or name]
    if "minimum" in spec and "maximum" in spec:
        parts.append(f"{spec['minimum']} to {spec['maximum']}")
    if spec.get("default") not in (None, ""):
        parts.append(f"default {spec['default']}")
    return {"type": kind, "description": ", ".join(str(p) for p in parts) + "."}


def build() -> list[dict[str, Any]]:
    targets = []
    for source_id, tool_cls in TOOLS_BY_SOURCE_ID.items():
        schema = tool_cls.InputSchema.model_json_schema()
        input_schema: dict[str, Any] = {
            "type": "object",
            "properties": {name: _property(name, spec) for name, spec in schema["properties"].items()},
        }
        if schema.get("required"):
            input_schema["required"] = list(schema["required"])
        targets.append(
            {
                "sourceId": source_id,
                "tool": {"name": TOOL_NAME, "description": tool_cls.description, "inputSchema": input_schema},
            }
        )
    return targets


def render() -> str:
    return json.dumps(build(), indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="exit 1 if the file is out of date")
    args = parser.parse_args()
    content = render()
    if args.check:
        current = OUTPUT.read_text() if OUTPUT.exists() else ""
        if current != content:
            print(f"{OUTPUT} is out of date; run scripts/gen_tool_schemas.py", file=sys.stderr)
            return 1
        return 0
    OUTPUT.write_text(content)
    print(f"wrote {OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
