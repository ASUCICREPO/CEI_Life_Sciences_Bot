from __future__ import annotations

from shared.config import config
from shared.tools.base import DataSourceTool
from shared.tools.data_sources.biorxiv import BioRxivTool
from shared.tools.data_sources.chembl import ChEMBLTool
from shared.tools.data_sources.clinical_trials import ClinicalTrialsTool
from shared.tools.data_sources.cms_coverage import CMSCoverageTool
from shared.tools.data_sources.icd10 import ICD10Tool
from shared.tools.data_sources.medrxiv import MedRxivTool
from shared.tools.data_sources.npi_registry import NPIRegistryTool
from shared.tools.data_sources.open_targets import OpenTargetsTool
from shared.tools.data_sources.openfda import OpenFDATool
from shared.tools.data_sources.patentsview import PatentsViewTool
from shared.tools.data_sources.pubchem import PubChemTool
from shared.tools.data_sources.pubmed import PubMedTool

# F4: Gateway target id (also the `cei-tools/src.<id>` scope suffix) -> tool class. The single source of
# truth for scripts/gen_tool_schemas.py and the data-source Lambda tests.
TOOLS_BY_SOURCE_ID: dict[str, type[DataSourceTool]] = {
    "pubmed": PubMedTool,
    "clinicaltrials": ClinicalTrialsTool,
    "biorxiv": BioRxivTool,
    "medrxiv": MedRxivTool,
    "chembl": ChEMBLTool,
    "opentargets": OpenTargetsTool,
    "cmscoverage": CMSCoverageTool,
    "icd10": ICD10Tool,
    "npi": NPIRegistryTool,
    "pubchem": PubChemTool,
    "openfda": OpenFDATool,
    "patentsview": PatentsViewTool,
}

ALL_DATA_SOURCE_TOOLS: list[DataSourceTool] = [
    PubMedTool(direct_mode=config.data_sources_direct_invoke),
    ClinicalTrialsTool(direct_mode=config.data_sources_direct_invoke),
    BioRxivTool(direct_mode=config.data_sources_direct_invoke),
    MedRxivTool(direct_mode=config.data_sources_direct_invoke),
    ChEMBLTool(direct_mode=config.data_sources_direct_invoke),
    OpenTargetsTool(direct_mode=config.data_sources_direct_invoke),
    CMSCoverageTool(direct_mode=config.data_sources_direct_invoke),
    ICD10Tool(direct_mode=config.data_sources_direct_invoke),
    NPIRegistryTool(direct_mode=config.data_sources_direct_invoke),
    PubChemTool(direct_mode=config.data_sources_direct_invoke),
    OpenFDATool(direct_mode=config.data_sources_direct_invoke),
    PatentsViewTool(direct_mode=config.data_sources_direct_invoke),
]


def all_langchain_tools() -> list:
    return [t.as_langchain_tool() for t in ALL_DATA_SOURCE_TOOLS]
