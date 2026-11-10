from types import SimpleNamespace

import pytest

import chemistry
import lab_calculations as lab
import protein_primer
import sequence_basics as seq
from handler import handler


def test_sequence_operations_and_frames():
    assert seq.validate_sequence("ACGTN")["valid"]
    assert not seq.validate_sequence("ACGU", "dna")["valid"]
    assert seq.transcribe("ATG") == {"sequence": "AUG"}
    assert seq.reverse_complement("ATGC")["sequence"] == "GCAT"
    assert seq.translate("ATGAAATAA")["protein"] == "MK*"
    assert seq.translate("TTATTTCAT", frame=4)["protein"] == "MK*"
    assert seq.gc_content("ACGT")["gc_fraction"] == 0.5
    assert seq.composition("AAGT")["counts"] == {"A": 2, "G": 1, "T": 1}
    assert any(orf["protein"] == "MK" for orf in seq.orf_finder("ATGAAATAA")["orfs"])


def test_protein_primers_and_restriction_sites():
    result = protein_primer.protein_properties("ACDE")
    assert result["molecular_weight_da"] > 0 and result["isoelectric_point"] > 0
    assert isinstance(protein_primer.primer_tm("ACGTACGTACGT" )["tm_celsius"], float)
    assert "structure_found" in protein_primer.primer_hairpin_dimer("ACGTACGTACGT")["hairpin"]
    assert protein_primer.restriction_sites("AAAGAATTCTTT", ["EcoRI"])["cuts"]["EcoRI"] == [5]


def test_chemistry_descriptors_and_similarity():
    assert chemistry.validate_structure("CCO")["valid"]
    assert not chemistry.validate_structure("not smiles")["valid"]
    assert chemistry.molecular_descriptors("CCO")["molecular_weight_da"] == pytest.approx(46.069, rel=0.001)
    assert chemistry.lipinski_check("CCO")["passes_all_rules"]
    assert chemistry.tanimoto_similarity("CCO", "CCO")["similarity"] == 1


def test_lab_math_notation_and_file_parsing():
    assert lab.molarity(1, 100, 0.5)["molarity_mol_l"] == pytest.approx(0.02)
    assert lab.dilution(10, 1, 100)["stock_volume"] == 10
    assert lab.unit_convert(1, "mL", "uL")["value"] == pytest.approx(1000)
    assert lab.hgvs_check("NM_000059.4:c.68_69del")["valid_syntax"]
    assert not lab.hgvs_check("invalid")["valid_syntax"]
    assert lab.parse_fasta_genbank(">test\nACGT\n")["records"][0]["sequence"] == "ACGT"
    with pytest.raises(ValueError):
        lab.dilution(1, 2, 100)


def test_gateway_dispatch_rejects_unknown_and_invalid_input():
    context = SimpleNamespace(client_context=SimpleNamespace(custom={"bedrockAgentCoreToolName": "lsops___dilution"}))
    assert handler({"stock_concentration": 10, "target_concentration": 1, "final_volume": 100}, context)["result"]["stock_volume"] == 10
    assert handler({"stock_concentration": 0, "target_concentration": 1, "final_volume": 100}, context)["error"]["code"] == "invalid_input"
    assert handler({}, SimpleNamespace())["error"]["code"] == "invalid_tool"
