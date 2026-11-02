"""Gateway Lambda target: ICD-10 (F4). Tool `icd10___search`."""

from __future__ import annotations

from data_source_runtime import make_handler
from shared.tools.data_sources.icd10 import ICD10Tool

handler = make_handler(ICD10Tool, source_id="icd10")
