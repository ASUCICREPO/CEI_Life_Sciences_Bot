from __future__ import annotations

from unittest.mock import Mock

from shared.schemas.citation import SourceType
from shared.tools.data_sources.openfda import OpenFDATool
from shared.tools.data_sources.patentsview import PatentsViewTool


def _resp(json_body: dict, status_code: int = 200) -> Mock:
    resp = Mock()
    resp.status_code = status_code
    resp.json.return_value = json_body
    resp.raise_for_status = Mock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = Exception(f"HTTP {status_code}")
    return resp


def test_openfda_direct_invoke_parses_drug_and_device_records(monkeypatch) -> None:
    drugsfda_resp = _resp({"results": [
        {
            "application_number": "NDA123",
            "sponsor_name": "Acme Pharma",
            "openfda": {"brand_name": ["Acmezol"], "generic_name": ["acmezolamide"]},
            "submission_status_date": "20210101",
        }
    ]})
    label_resp = _resp({}, status_code=404)
    device_resp = _resp({"results": [
        {"k_number": "K210001", "device_name": "Acme Device", "applicant": "Acme Devices Inc", "decision_date": "20200505"}
    ]})

    calls = iter([drugsfda_resp, label_resp, device_resp])
    monkeypatch.setattr("shared.tools.data_sources.openfda.requests.get", lambda *a, **k: next(calls))

    tool = OpenFDATool(direct_mode=True)
    result = tool.invoke(query="acmezolamide")

    assert result.source_type == SourceType.OPENFDA
    kinds = {r["kind"] for r in result.results}
    assert "drug_approval" in kinds
    assert "device_510k" in kinds
    assert len(result.citations) == len(result.results)
    for citation in result.citations:
        assert citation.url.startswith("https://")


def test_openfda_direct_invoke_never_raises_on_http_error(monkeypatch) -> None:
    def _raise(*_a, **_k):
        raise RuntimeError("network down")

    monkeypatch.setattr("shared.tools.data_sources.openfda.requests.get", _raise)
    tool = OpenFDATool(direct_mode=True)
    result = tool.invoke(query="acmezolamide")

    assert result.results == []
    assert result.citations == []
    assert len(result.query_metadata["errors"]) == 3


def test_patentsview_missing_key_returns_empty_result_with_note(monkeypatch) -> None:
    monkeypatch.delenv("CEI_PATENTSVIEW_API_KEY", raising=False)
    tool = PatentsViewTool(direct_mode=True)
    result = tool.invoke(query="CRISPR gene editing")

    assert result.results == []
    assert result.citations == []
    assert "CEI_PATENTSVIEW_API_KEY" in result.query_metadata["note"]


def test_patentsview_direct_invoke_parses_patents_with_key(monkeypatch) -> None:
    monkeypatch.setenv("CEI_PATENTSVIEW_API_KEY", "test-key")
    resp = _resp({"patents": [
        {
            "patent_id": "11000000",
            "patent_title": "Gene editing method",
            "patent_abstract": "A method for editing genes using CRISPR.",
            "patent_date": "2021-06-15",
            "assignees": [{"assignee_organization": "Acme Biotech"}],
        }
    ]})
    monkeypatch.setattr("shared.tools.data_sources.patentsview.requests.get", lambda *a, **k: resp)

    tool = PatentsViewTool(direct_mode=True)
    result = tool.invoke(query="CRISPR gene editing")

    assert len(result.results) == 1
    assert result.citations[0].source_id == "PATENT:11000000"
    assert result.citations[0].url.startswith("https://")
    assert result.citations[0].year == 2021


def test_patentsview_direct_invoke_never_raises_on_http_error(monkeypatch) -> None:
    monkeypatch.setenv("CEI_PATENTSVIEW_API_KEY", "test-key")

    def _raise(*_a, **_k):
        raise RuntimeError("network down")

    monkeypatch.setattr("shared.tools.data_sources.patentsview.requests.get", _raise)
    tool = PatentsViewTool(direct_mode=True)
    result = tool.invoke(query="CRISPR")

    assert result.results == []
    assert result.citations == []
    assert "error" in result.query_metadata
