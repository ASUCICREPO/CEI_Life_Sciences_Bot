"""Gateway Lambda target: ClinicalTrials.gov (F4). Tool `clinicaltrials___search`."""

from __future__ import annotations

from data_source_runtime import make_handler
from shared.tools.data_sources.clinical_trials import ClinicalTrialsTool

handler = make_handler(ClinicalTrialsTool, source_id="clinicaltrials")
