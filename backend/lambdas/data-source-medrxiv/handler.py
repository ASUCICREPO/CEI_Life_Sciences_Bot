"""Gateway Lambda target: medRxiv (F4). Tool `medrxiv___search`."""

from __future__ import annotations

from data_source_runtime import make_handler
from shared.tools.data_sources.medrxiv import MedRxivTool

handler = make_handler(MedRxivTool, source_id="medrxiv")
