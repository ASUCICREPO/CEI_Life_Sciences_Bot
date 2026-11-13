"""Gateway Lambda target: Open Targets (F4). Tool `opentargets___search`."""

from __future__ import annotations

from data_source_runtime import make_handler
from shared.tools.data_sources.open_targets import OpenTargetsTool

handler = make_handler(OpenTargetsTool, source_id="opentargets")
