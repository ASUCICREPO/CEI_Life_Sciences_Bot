from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from shared.tools.data_sources import TOOLS_BY_SOURCE_ID

BACKEND = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("gen_tool_schemas", BACKEND / "scripts" / "gen_tool_schemas.py")
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)


def test_generated_schema_file_is_current():
    """The Gateway advertises what the Lambda validates: re-run scripts/gen_tool_schemas.py if this fails."""
    assert gen.OUTPUT.read_text() == gen.render()


def test_one_lambda_dir_per_registered_source():
    dirs = {p.name.removeprefix("data-source-") for p in (BACKEND / "lambdas").glob("data-source-*") if p.is_dir()}
    assert dirs == set(TOOLS_BY_SOURCE_ID)


def test_schema_properties_and_required_match_input_schemas():
    for entry in json.loads(gen.OUTPUT.read_text()):
        model = TOOLS_BY_SOURCE_ID[entry["sourceId"]].InputSchema.model_json_schema()
        schema = entry["tool"]["inputSchema"]
        assert entry["tool"]["name"] == "search"
        assert set(schema["properties"]) == set(model["properties"])
        assert schema.get("required", []) == model.get("required", [])
        # only keys the Gateway SchemaDefinition accepts
        assert set(schema) <= {"type", "properties", "required"}
        for prop in schema["properties"].values():
            assert set(prop) <= {"type", "description"}


def test_source_ids_are_scope_safe():
    """B69: Cedar `like` is a substring match, so no source id (scope suffix) may contain another."""
    ids = list(TOOLS_BY_SOURCE_ID) + ["ncbi", "common"]
    clashes = [(a, b) for a in ids for b in ids if a != b and a in b]
    assert clashes == []
