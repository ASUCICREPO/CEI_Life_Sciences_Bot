"""Biopython and Primer3 sequence properties."""
from Bio.Restriction import RestrictionBatch
from Bio.Seq import Seq
from Bio.SeqUtils.ProtParam import ProteinAnalysis
from Bio.SeqUtils import MeltingTemp
import primer3

from sequence_basics import sequence


def protein_properties(seq):
    protein = sequence(seq, "protein")
    if set(protein) - set("ACDEFGHIKLMNPQRSTVWY"):
        raise ValueError("protein properties require unambiguous amino acids")
    analysis = ProteinAnalysis(protein)
    reduced, oxidized = analysis.molar_extinction_coefficient()
    return {"molecular_weight_da": analysis.molecular_weight(), "isoelectric_point": analysis.isoelectric_point(),
            "extinction_coefficient_reduced": reduced, "extinction_coefficient_oxidized": oxidized,
            "gravy": analysis.gravy()}


def _primer(value):
    primer = sequence(value)
    if set(primer) - set("ACGT") or not 2 <= len(primer) <= 60:
        raise ValueError("primer must contain 2 to 60 unambiguous DNA bases")
    return primer


def primer_tm(seq):
    return {"tm_celsius": MeltingTemp.Tm_NN(_primer(seq)), "method": "nearest-neighbor", "assumptions": "Biopython defaults"}


def primer_hairpin_dimer(seq, other_seq=None):
    first = _primer(seq)
    hairpin = primer3.calc_hairpin(first)
    dimer = primer3.calc_heterodimer(first, _primer(other_seq)) if other_seq is not None else primer3.calc_homodimer(first)
    return {"hairpin": {"structure_found": hairpin.structure_found, "tm_celsius": hairpin.tm, "dg_cal_mol": hairpin.dg},
            "dimer": {"structure_found": dimer.structure_found, "tm_celsius": dimer.tm, "dg_cal_mol": dimer.dg},
            "assumptions": "Primer3 default ionic and oligonucleotide concentrations"}


def restriction_sites(seq, enzymes):
    if not isinstance(enzymes, list) or not 1 <= len(enzymes) <= 30 or not all(isinstance(e, str) for e in enzymes):
        raise ValueError("provide 1 to 30 enzyme names")
    result = RestrictionBatch(enzymes).search(Seq(sequence(seq)))
    return {"cuts": {str(enzyme): sites for enzyme, sites in result.items()},
            "coordinate_system": "Biopython one-based cut position"}
