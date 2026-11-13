from shared.tools.data_sources.cms_coverage import CMSCoverageTool, _DETAIL, _REPORT


class Response:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


def test_cms_coverage_searches_ncd_report_and_maps_detail(monkeypatch):
    report = {"data": [
        {"document_id": 11, "document_version": 2, "document_display_id": "30.3", "title": "Acupuncture", "last_updated": "05/25/2022"},
        {"document_id": 108, "document_version": 1, "document_display_id": "100.3", "title": "Esophageal pH Monitoring"},
    ]}
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        if url == _REPORT:
            return Response(report)
        assert url == _DETAIL
        assert kwargs["params"] == {"ncdid": 11, "ncdver": 2}
        return Response({"data": [{"effective_date": "01/21/2020", "indications_limitations": "<p>Coverage &amp; limits</p>"}]})

    monkeypatch.setattr("shared.tools.data_sources.cms_coverage.http.get", get)
    result = CMSCoverageTool().fetch("acupuncture")
    assert len(calls) == 2
    assert result.results[0]["ncd_id"] == "11"
    assert result.results[0]["effective_date"] == "01/21/2020"
    assert result.citations[0].year == 2020
    assert result.citations[0].relevant_passage == "Coverage & limits"
    assert "NCDId=11" in result.citations[0].url


def test_cms_report_failure_is_redacted(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("sensitive upstream detail")

    monkeypatch.setattr("shared.tools.data_sources.cms_coverage.http.get", fail)
    result = CMSCoverageTool().fetch("MRI")
    assert not result.results
    assert "sensitive" not in str(result.query_metadata)


def test_cms_reports_missing_details_without_fabricating_dates(monkeypatch):
    def get(url, **kwargs):
        if url == _REPORT:
            return Response({"data": [{"document_id": 11, "document_version": 1, "title": "Acupuncture"}]})
        raise RuntimeError("detail unavailable")

    monkeypatch.setattr("shared.tools.data_sources.cms_coverage.http.get", get)
    result = CMSCoverageTool().fetch("acupuncture")
    assert result.results[0]["detail_available"] is False
    assert result.results[0]["effective_date"] == ""
    assert result.citations[0].year == 0
