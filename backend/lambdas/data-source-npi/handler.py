"""Gateway Lambda target: NPI Registry (F4). Tool `npi___search`."""

from __future__ import annotations

from data_source_runtime import make_handler
from shared.tools.data_sources.npi_registry import NPIRegistryTool

handler = make_handler(NPIRegistryTool, source_id="npi")
