import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

spec = importlib.util.spec_from_file_location("ncbi_targets", Path(__file__).with_name("targets.py"))
ncbi = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ncbi)


def test_taxonomy_resolves_name_and_fetches_lineage():
    acquire = Mock()
    request = Mock(side_effect=[json.dumps({"esearchresult": {"idlist": ["9606"]}}),
        '<?xml version="1.0"?><!DOCTYPE TaxaSet SYSTEM "https://example.invalid/taxa.dtd">'
        '<TaxaSet><Taxon><TaxId>9606</TaxId><ScientificName>Homo sapiens</ScientificName>'
        '<Rank>species</Rank><LineageEx><Taxon><TaxId>9605</TaxId>'
        '<ScientificName>Homo</ScientificName><Rank>genus</Rank></Taxon></LineageEx></Taxon></TaxaSet>'])
    result = ncbi.taxonomy_lineage("Homo sapiens", acquire=acquire, request=request)
    assert result["tax_id"] == "9606" and result["lineage"][0]["name"] == "Homo"
    assert acquire.call_count == 2


def test_ambiguous_taxon_does_not_guess():
    request = Mock(return_value=json.dumps({"esearchresult": {"idlist": ["1", "2"]}}))
    with pytest.raises(ValueError):
        ncbi.taxonomy_lineage("Homo", acquire=Mock(), request=request)
    assert request.call_count == 1


def test_sequence_fetch_validates_and_limits():
    request = Mock(return_value=">NC_000001.11\nACGT\n")
    out = ncbi.sequence_efetch("NC_000001.11", database="nuccore", format="fasta", acquire=Mock(), request=request)
    assert out["record"].startswith(">") and out["accession"] == "NC_000001.11"
    with pytest.raises(ValueError):
        ncbi.sequence_efetch("../../url", database="nuccore", format="fasta", acquire=Mock(), request=request)


def test_blast_submit_and_poll_wait_then_ready():
    acquire = Mock()
    request = Mock(side_effect=["RID = ABC12345\nRTOE = 22\n", "Status=WAITING\n", "<?xml version='1.0'?><BlastOutput />"])
    submit = ncbi.blast_submit("ACGT", program="blastn", database="core_nt", contact_email="ops@example.com",
                               acquire=acquire, request=request)
    assert submit["poll_after_seconds"] == 60
    assert ncbi.blast_poll(submit["rid"], contact_email="ops@example.com", acquire=acquire, request=request)["status"] == "waiting"
    assert ncbi.blast_poll(submit["rid"], contact_email="ops@example.com", acquire=acquire, request=request)["status"] == "ready"
    assert acquire.call_args_list[-1].args == ("blast", "ABC12345")


def test_limiter_failure_prevents_network():
    acquire, request = Mock(side_effect=RuntimeError("rate limited")), Mock()
    with pytest.raises(RuntimeError):
        ncbi.blast_poll("ABC12345", contact_email="ops@example.com", acquire=acquire, request=request)
    request.assert_not_called()


def test_xml_entity_and_unknown_blast_response_rejected():
    with pytest.raises(ValueError):
        ncbi._safe_xml('<!DOCTYPE x [<!ENTITY x "secret">]><x>&x;</x>')
    with pytest.raises(ValueError):
        ncbi.blast_poll("ABC12345", contact_email="ops@example.com", acquire=Mock(), request=Mock(return_value="bad response"))
