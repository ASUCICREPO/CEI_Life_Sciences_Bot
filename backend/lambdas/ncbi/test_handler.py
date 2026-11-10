from types import SimpleNamespace
from unittest.mock import Mock

import handler as target
from limiter import RateLimited


def context(name):
    return SimpleNamespace(client_context=SimpleNamespace(custom={"bedrockAgentCoreToolName": f"ncbi___{name}"}))


def test_taxonomy_and_sequence_dispatch(monkeypatch):
    acquire = Mock()
    monkeypatch.setattr(target, "limiter", lambda: SimpleNamespace(acquire=acquire))
    taxonomy = Mock(return_value={"tax_id": "9606"})
    sequence = Mock(return_value={"accession": "NM_000001"})
    monkeypatch.setattr(target, "taxonomy_lineage", taxonomy)
    monkeypatch.setattr(target, "sequence_efetch", sequence)
    assert target.handler({"taxon": "Homo sapiens"}, context("taxonomy_lineage"))["result"]["tax_id"] == "9606"
    assert target.handler({"accession": "NM_000001", "database": "nuccore", "format": "fasta"},
                          context("sequence_efetch"))["result"]["accession"] == "NM_000001"
    taxonomy.assert_called_once_with("Homo sapiens", acquire=acquire)
    sequence.assert_called_once_with("NM_000001", database="nuccore", format="fasta", acquire=acquire)


def test_invalid_args_and_blast_do_not_reach_upstream(monkeypatch):
    monkeypatch.setattr(target, "limiter", lambda: SimpleNamespace(acquire=Mock()))
    taxonomy = Mock()
    monkeypatch.setattr(target, "taxonomy_lineage", taxonomy)
    assert target.handler({"taxon": "human", "user_id": "spoofed"}, context("taxonomy_lineage"))["error"]["code"] == "invalid_request"
    assert target.handler({"rid": "RID123"}, context("blast_poll"))["error"]["code"] == "invalid_request"
    taxonomy.assert_not_called()


def test_rate_limited_or_failed_upstream_is_redacted(monkeypatch):
    monkeypatch.setattr(target, "limiter", lambda: SimpleNamespace(acquire=Mock()))
    monkeypatch.setattr(target, "taxonomy_lineage", Mock(side_effect=RateLimited()))
    assert target.handler({"taxon": "human"}, context("taxonomy_lineage"))["error"]["code"] == "rate_limited"
    monkeypatch.setattr(target, "taxonomy_lineage", Mock(side_effect=RuntimeError("private query")))
    result = target.handler({"taxon": "human"}, context("taxonomy_lineage"))
    assert result["error"]["code"] == "service_unavailable" and "private query" not in str(result)
