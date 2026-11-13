"""AgentCore Gateway Lambda target for deterministic life-sciences operations."""
import inspect
import json

import chemistry
import lab_calculations
import protein_primer
import sequence_basics

OPERATIONS = {
    "validate_sequence": sequence_basics.validate_sequence,
    "transcribe": sequence_basics.transcribe,
    "reverse_complement": sequence_basics.reverse_complement,
    "translate": sequence_basics.translate,
    "orf_finder": sequence_basics.orf_finder,
    "gc_content": sequence_basics.gc_content,
    "composition": sequence_basics.composition,
    "protein_properties": protein_primer.protein_properties,
    "primer_tm": protein_primer.primer_tm,
    "primer_hairpin_dimer": protein_primer.primer_hairpin_dimer,
    "restriction_sites": protein_primer.restriction_sites,
    "validate_structure": chemistry.validate_structure,
    "molecular_descriptors": chemistry.molecular_descriptors,
    "lipinski_check": chemistry.lipinski_check,
    "tanimoto_similarity": chemistry.tanimoto_similarity,
    "molarity": lab_calculations.molarity,
    "dilution": lab_calculations.dilution,
    "unit_convert": lab_calculations.unit_convert,
    "hgvs_check": lab_calculations.hgvs_check,
    "parse_fasta_genbank": lab_calculations.parse_fasta_genbank,
}


def handler(event, context):
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    name = custom.get("bedrockAgentCoreToolName", "")
    if not isinstance(name, str) or "___" not in name:
        return {"error": {"code": "invalid_tool", "message": "Missing Gateway tool name."}}
    operation = OPERATIONS.get(name.split("___", 1)[1])
    if operation is None:
        return {"error": {"code": "invalid_tool", "message": "Unknown operation."}}
    if not isinstance(event, dict):
        return {"error": {"code": "invalid_input", "message": "Expected an argument object."}}
    try:
        inspect.signature(operation).bind(**event)
        result = operation(**event)
        # Reject non-finite calculations rather than emitting invalid JSON.
        json.dumps(result, allow_nan=False)
        return {"result": result}
    except (ValueError, TypeError, KeyError, OverflowError, ZeroDivisionError):
        return {"error": {"code": "invalid_input", "message": "Invalid arguments for operation."}}
