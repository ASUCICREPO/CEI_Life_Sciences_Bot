"""Chemical structure and descriptors using RDKit."""
from rdkit import Chem, DataStructs, RDLogger
from rdkit.Chem import Descriptors, Lipinski, rdMolDescriptors, rdFingerprintGenerator

# RDKit's parser diagnostics can include the user's input structure.
RDLogger.DisableLog("rdApp.error")


def molecule(structure, format="smiles"):
    if not isinstance(structure, str) or not 1 <= len(structure) <= 10_000:
        raise ValueError("invalid structure length")
    if format == "smiles":
        mol = Chem.MolFromSmiles(structure)
    elif format == "inchi":
        mol = Chem.MolFromInchi(structure)
    else:
        raise ValueError("format must be smiles or inchi")
    if mol is None:
        raise ValueError("invalid structure")
    return mol


def validate_structure(structure, format="smiles"):
    try:
        mol = molecule(structure, format)
    except ValueError:
        return {"valid": False}
    return {"valid": True, "canonical_smiles": Chem.MolToSmiles(mol)}


def molecular_descriptors(structure, format="smiles"):
    mol = molecule(structure, format)
    return {"molecular_weight_da": Descriptors.MolWt(mol), "logp": Descriptors.MolLogP(mol),
            "tpsa_angstrom_squared": rdMolDescriptors.CalcTPSA(mol),
            "hydrogen_bond_donors": Lipinski.NumHDonors(mol), "hydrogen_bond_acceptors": Lipinski.NumHAcceptors(mol)}


def lipinski_check(structure, format="smiles"):
    values = molecular_descriptors(structure, format)
    violations = [name for name, failed in [("molecular_weight_gt_500", values["molecular_weight_da"] > 500),
        ("logp_gt_5", values["logp"] > 5), ("donors_gt_5", values["hydrogen_bond_donors"] > 5),
        ("acceptors_gt_10", values["hydrogen_bond_acceptors"] > 10)] if failed]
    return {"passes_all_rules": not violations, "violations": violations, "descriptors": values}


def tanimoto_similarity(structure_a, structure_b, format="smiles"):
    generator = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=2048)
    left, right = molecule(structure_a, format), molecule(structure_b, format)
    return {"similarity": DataStructs.TanimotoSimilarity(generator.GetFingerprint(left), generator.GetFingerprint(right)),
            "fingerprint": "Morgan radius 2, 2048 bits"}
