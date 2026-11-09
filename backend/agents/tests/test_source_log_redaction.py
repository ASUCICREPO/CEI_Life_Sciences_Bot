"""Upstream exceptions can contain user queries and credentials in URLs."""
import importlib

import pytest

from shared.tools import retrying_http


@pytest.mark.parametrize("module,tool", [
    ("biorxiv", "BioRxivTool"), ("chembl", "ChEMBLTool"),
    ("clinical_trials", "ClinicalTrialsTool"), ("cms_coverage", "CMSCoverageTool"),
    ("icd10", "ICD10Tool"), ("medrxiv", "MedRxivTool"), ("npi_registry", "NPIRegistryTool"),
    ("openfda", "OpenFDATool"), ("open_targets", "OpenTargetsTool"),
    ("patentsview", "PatentsViewTool"), ("pubchem", "PubChemTool"), ("pubmed", "PubMedTool"),
])
def test_source_error_log_excludes_upstream_exception_message(module, tool, monkeypatch, caplog):
    def fail(*args, **kwargs):
        raise RuntimeError("private-query-and-api-key")

    monkeypatch.setattr(retrying_http, "get", fail)
    monkeypatch.setattr(retrying_http, "post", fail)
    monkeypatch.setenv("CEI_PATENTSVIEW_API_KEY", "configured")
    source = importlib.import_module(f"shared.tools.data_sources.{module}")
    with caplog.at_level("ERROR"):
        getattr(source, tool)().fetch(query="example")
    assert "RuntimeError" in caplog.text
    assert "private-query-and-api-key" not in caplog.text
