"""Gateway Lambda target: CMS Coverage Database (F4). Tool `cmscoverage___search`."""

from __future__ import annotations

from data_source_runtime import make_handler
from shared.tools.data_sources.cms_coverage import CMSCoverageTool

handler = make_handler(CMSCoverageTool, source_id="cmscoverage")
