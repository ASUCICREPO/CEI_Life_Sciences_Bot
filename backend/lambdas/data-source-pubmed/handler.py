"""Gateway Lambda target: PubMed (F4). Tool `pubmed___search`.

The user key secret is `cei/user-keys/{user_id}/ncbi`: an NCBI account key, shared with the
F15 NCBI targets (B27/B85)."""

from __future__ import annotations

from data_source_runtime import make_handler
from shared.tools.data_sources.pubmed import PubMedTool

handler = make_handler(PubMedTool, source_id="pubmed", api_key_source_id="ncbi")
