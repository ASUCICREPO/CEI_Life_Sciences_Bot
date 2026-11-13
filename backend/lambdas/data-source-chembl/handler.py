"""Gateway Lambda target: ChEMBL (F4). Tool `chembl___search`."""

from __future__ import annotations

from data_source_runtime import make_handler
from shared.tools.data_sources.chembl import ChEMBLTool

handler = make_handler(ChEMBLTool, source_id="chembl")
