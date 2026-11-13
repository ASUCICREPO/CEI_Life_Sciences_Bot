"""Sequence operations. Coordinates are zero-based, half-open."""
from collections import Counter

from Bio.Data import IUPACData
from Bio.Seq import Seq
from Bio.SeqUtils import gc_fraction

MAX_SEQUENCE = 100_000
ALPHABETS = {
    "dna": set(IUPACData.ambiguous_dna_letters.upper()),
    "rna": set(IUPACData.ambiguous_rna_letters.upper()),
    "protein": set(IUPACData.extended_protein_letters.upper()) | {"*"},
}


def sequence(value, kind="dna"):
    if kind not in ALPHABETS or not isinstance(value, str):
        raise ValueError("unsupported sequence type or input")
    result = "".join(value.split()).upper()
    if not result or len(result) > MAX_SEQUENCE or set(result) - ALPHABETS[kind]:
        raise ValueError("empty, oversized, or invalid IUPAC sequence")
    return result


def validate_sequence(seq, kind="dna"):
    try:
        normalized = sequence(seq, kind)
    except ValueError:
        return {"valid": False, "kind": kind}
    return {"valid": True, "kind": kind, "length": len(normalized)}


def transcribe(seq):
    return {"sequence": str(Seq(sequence(seq)).transcribe())}


def reverse_complement(seq, kind="dna"):
    if kind not in ("dna", "rna"):
        raise ValueError("kind must be dna or rna")
    source = Seq(sequence(seq, kind))
    return {"sequence": str(source.reverse_complement_rna() if kind == "rna" else source.reverse_complement())}


def translate(seq, frame=1, table=1):
    dna = sequence(seq)
    if isinstance(frame, bool) or frame not in range(1, 7):
        raise ValueError("frame must be 1 through 6")
    strand = Seq(dna) if frame <= 3 else Seq(dna).reverse_complement()
    offset = (frame - 1) % 3
    coding = strand[offset:]
    coding = coding[:len(coding) - len(coding) % 3]
    return {"protein": str(coding.translate(table=table)), "frame": frame, "codon_table": table}


def orf_finder(seq, min_codons=1, table=1):
    from Bio.Data import CodonTable
    dna = sequence(seq)
    if isinstance(min_codons, bool) or not isinstance(min_codons, int) or min_codons < 1:
        raise ValueError("min_codons must be a positive integer")
    codon_table = CodonTable.unambiguous_dna_by_id[table]
    found = []
    for frame in range(1, 7):
        strand = dna if frame <= 3 else str(Seq(dna).reverse_complement())
        offset = (frame - 1) % 3
        active = []
        for i in range(offset, len(strand) - 2, 3):
            codon = strand[i:i + 3]
            if codon in codon_table.start_codons:
                active.append(i)
            if codon in codon_table.stop_codons:
                for start in active:
                    if (i - start) // 3 >= min_codons:
                        left, right = (start, i + 3) if frame <= 3 else (len(dna) - i - 3, len(dna) - start)
                        found.append({"frame": frame, "start": left, "end": right, "protein": str(Seq(strand[start:i]).translate(table=table))})
                        if len(found) > 1000:
                            raise ValueError("too many ORFs; increase min_codons")
                active = []
    return {"orfs": found, "coordinate_system": "zero-based half-open"}


def gc_content(seq, kind="dna"):
    if kind not in ("dna", "rna"):
        raise ValueError("GC content requires DNA or RNA")
    return {"gc_fraction": gc_fraction(sequence(seq, kind)), "ambiguity_handling": "remove"}


def composition(seq, kind="dna"):
    normalized = sequence(seq, kind)
    return {"length": len(normalized), "counts": dict(sorted(Counter(normalized).items()))}
