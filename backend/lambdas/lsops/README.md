# Life sciences operations target

This target is deterministic and makes no external data or model calls. Gateway
must supply `bedrockAgentCoreToolName` in Lambda client context; the event object
contains only arguments to that tool. It must be attached to the
`cei-tools/common` scope. This directory supplies the handler and container;
Gateway schemas, scope policy, CDK wiring, and agent integration are separate.

| Tool suffix | Arguments |
| --- | --- |
| validate_sequence | `seq`, optional `kind` (`dna`, `rna`, `protein`) |
| transcribe | `seq` (DNA) |
| reverse_complement | `seq`, optional `kind` (`dna`, `rna`) |
| translate | `seq` (DNA), optional `frame` (1–6), `table` (NCBI table ID) |
| orf_finder | `seq` (DNA), optional `min_codons`, `table` (NCBI table ID) |
| gc_content | `seq`, optional `kind` (`dna`, `rna`) |
| composition | `seq`, optional `kind` (`dna`, `rna`, `protein`) |
| protein_properties | `seq` (unambiguous protein) |
| primer_tm | `seq` (unambiguous DNA) |
| primer_hairpin_dimer | `seq`, optional `other_seq` (unambiguous DNA) |
| restriction_sites | `seq` (DNA), `enzymes` (names, e.g. `EcoRI`) |
| validate_structure | `structure`, optional `format` (`smiles`, `inchi`) |
| molecular_descriptors | `structure`, optional `format` |
| lipinski_check | `structure`, optional `format` |
| tanimoto_similarity | `structure_a`, `structure_b`, optional shared `format` |
| molarity | `mass_g`, `molecular_weight_g_mol`, `volume_l` |
| dilution | `stock_concentration`, `target_concentration`, `final_volume` |
| unit_convert | `value`, `from_unit`, `to_unit` |
| hgvs_check | `variant` |
| parse_fasta_genbank | `content`, optional `format` (`fasta`, `genbank`) |

Frames 1–3 are forward offsets 0–2; 4–6 are reverse-complement offsets 0–2.
ORF positions are zero-based, half-open coordinates on the original sequence.
Only complete ORFs with a start and in-frame stop codon are included. GC content
uses Biopython's default ambiguity handling. Primer thermodynamic calculations
use library default concentrations, and the result reports that assumption.
The HGVS operation checks syntax only; it does not validate the reference or
clinical interpretation. Sequence and file inputs are bounded.

Install the pinned dependencies from `requirements.txt` plus `hgvs==1.5.6`
without its optional reference-data dependencies, then run
`pytest backend/lambdas/lsops/tests` with `backend/lambdas/lsops` on
`PYTHONPATH`. Container build, cold start, dependency behavior, and Gateway
integration require validation in the CodeBuild/development environment.

The target construct builds `Dockerfile` as a CDK image asset. The current
`cei-life-sciences-deploy` CodeBuild project uses a non-VPC standard image with
privileged mode disabled; Docker support depends on its environment and must be
verified during the CodeBuild image-asset build. Verify ECR asset permissions
and the deployed image before relying on the target. The runtime and cold-start
timing have not been validated by the unit tests.
