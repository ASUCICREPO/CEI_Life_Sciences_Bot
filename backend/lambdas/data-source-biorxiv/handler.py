"""Gateway Lambda target: bioRxiv (F4). Tool `biorxiv___search`."""

from __future__ import annotations

from data_source_runtime import make_handler
from shared.tools.data_sources.biorxiv import BioRxivTool

handler = make_handler(BioRxivTool, source_id="biorxiv")
