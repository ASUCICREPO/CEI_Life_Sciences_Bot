"""Gateway Lambda target: PubChem (F4). Tool `pubchem___search`."""

from __future__ import annotations

from data_source_runtime import make_handler
from shared.tools.data_sources.pubchem import PubChemTool

handler = make_handler(PubChemTool, source_id="pubchem")
