"""Explicit-unit lab math and local sequence-file parsing."""
import io
import math

from Bio import SeqIO

_UNITS = {
    "L": ("volume", 1), "mL": ("volume", 1e-3), "uL": ("volume", 1e-6), "nL": ("volume", 1e-9),
    "g": ("mass", 1), "mg": ("mass", 1e-3), "ug": ("mass", 1e-6), "ng": ("mass", 1e-9),
    "mol": ("amount", 1), "mmol": ("amount", 1e-3), "umol": ("amount", 1e-6), "nmol": ("amount", 1e-9),
    "M": ("concentration", 1), "mM": ("concentration", 1e-3), "uM": ("concentration", 1e-6), "nM": ("concentration", 1e-9),
}


def _number(value, name, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    if value < 0 or (positive and value == 0):
        raise ValueError(f"{name} must be {'positive' if positive else 'nonnegative'}")
    return value


def molarity(mass_g, molecular_weight_g_mol, volume_l):
    mass = _number(mass_g, "mass_g")
    mw = _number(molecular_weight_g_mol, "molecular_weight_g_mol", True)
    volume = _number(volume_l, "volume_l", True)
    return {"molarity_mol_l": mass / mw / volume}


def dilution(stock_concentration, target_concentration, final_volume):
    c1 = _number(stock_concentration, "stock_concentration", True)
    c2 = _number(target_concentration, "target_concentration")
    v2 = _number(final_volume, "final_volume", True)
    if c2 > c1:
        raise ValueError("target concentration cannot exceed stock concentration by dilution")
    v1 = c2 * v2 / c1
    return {"stock_volume": v1, "diluent_volume": v2 - v1,
            "units": "volume uses the same unit as final_volume; concentrations must use the same unit"}


def unit_convert(value, from_unit, to_unit):
    if from_unit not in _UNITS or to_unit not in _UNITS or _UNITS[from_unit][0] != _UNITS[to_unit][0]:
        raise ValueError("unknown or incompatible units")
    return {"value": _number(value, "value") * _UNITS[from_unit][1] / _UNITS[to_unit][1], "unit": to_unit}


def hgvs_check(variant):
    # Grammar parsing checks notation only: it never checks reference sequence or clinical meaning.
    from hgvs.parser import Parser
    if not isinstance(variant, str) or not 1 <= len(variant) <= 500:
        return {"valid_syntax": False, "reference_verified": False}
    try:
        parsed = Parser().parse_hgvs_variant(variant)
    except Exception:
        return {"valid_syntax": False, "reference_verified": False}
    return {"valid_syntax": True, "normalized_syntax": str(parsed), "reference_verified": False}


def parse_fasta_genbank(content, format="fasta"):
    if format not in ("fasta", "genbank") or not isinstance(content, str) or not 1 <= len(content) <= 200_000:
        raise ValueError("invalid format or input size")
    records = []
    for record in SeqIO.parse(io.StringIO(content), format):
        if len(records) >= 100:
            raise ValueError("at most 100 sequence records")
        records.append({"id": record.id, "description": record.description, "sequence": str(record.seq), "length": len(record)})
    if not records:
        raise ValueError("no sequence records found")
    return {"format": format, "records": records}
