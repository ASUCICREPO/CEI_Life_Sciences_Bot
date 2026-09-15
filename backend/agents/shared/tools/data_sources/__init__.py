from __future__ import annotations

from shared.tools.base import DataSourceTool
from shared.tools.data_sources.biorxiv import BioRxivTool
from shared.tools.data_sources.chembl import ChEMBLTool
from shared.tools.data_sources.clinical_trials import ClinicalTrialsTool
from shared.tools.data_sources.cms_coverage import CMSCoverageTool
from shared.tools.data_sources.icd10 import ICD10Tool
from shared.tools.data_sources.medrxiv import MedRxivTool
from shared.tools.data_sources.npi_registry import NPIRegistryTool
from shared.tools.data_sources.open_targets import OpenTargetsTool
from shared.tools.data_sources.pubchem import PubChemTool
from shared.tools.data_sources.pubmed import PubMedTool

ALL_DATA_SOURCE_TOOLS: list[DataSourceTool] = [
    PubMedTool(),
    ClinicalTrialsTool(),
    BioRxivTool(),
    MedRxivTool(),
    ChEMBLTool(),
    OpenTargetsTool(),
    CMSCoverageTool(),
    ICD10Tool(),
    NPIRegistryTool(),
    PubChemTool(),
]


def all_langchain_tools() -> list:
    return [t.as_langchain_tool() for t in ALL_DATA_SOURCE_TOOLS]
