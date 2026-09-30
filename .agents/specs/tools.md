# Tools and Gateway Targets

Status: planned. Source of truth: `.agents/decision-log.md`. See `architecture.md` for where the Gateway sits, and `auth-and-tool-scope.md` for the Cedar scope model that gates every target below. `data-model.md` (sibling spec) owns the DynamoDB/S3 shapes referenced here.

**MCP tool naming convention (Proposal, not decided):** `"<target>___<tool>"`, matching the Policy in AgentCore action-name format (research facts: Cedar action names use this separator; `like` matching is substring-based, so target names must not be substrings of each other — see `auth-and-tool-scope.md` §3).

## 1. The 10 Data Source Lambdas (B19, D2)

All ten exist today as Python classes under `backend/agents/shared/tools/data_sources/`, subclassing `DataSourceTool` (`shared/tools/base.py`). Each has a working `_direct_invoke()` (direct HTTP call, used for local/CLI testing, `CEI_DIRECT_MODE=true`) and a `_build_payload()` for the Lambda-invoke path (B19: production removes `CEI_DIRECT_MODE`; the Lambda path becomes a Gateway target instead of a raw `lambda:invoke`).

| Source | Current `function_name` | Proposed MCP tool name | Inputs | Citation fields | Upstream rate limit | Phase |
|---|---|---|---|---|---|---|
| PubMed | `cei-data-source-pubmed` | `pubmed___search` | `query`, `max_results` (1–100) | `source_id` (`PMID:...`), `url`, `title`, `authors`, `year`, `relevant_passage`, `retrieved_at` | NCBI E-utilities: 3 req/s without a key, 10 req/s with one (B22 note) | F4 |
| ClinicalTrials.gov | `cei-data-source-clinical-trials` | `clinicaltrials___search` | `query`, `max_results` | same `Citation` shape (`shared/schemas/citation.py`) | TBD — not documented in decision log | F4 |
| bioRxiv | `cei-data-source-biorxiv` | `biorxiv___search` | `query`, `max_results` | same | TBD | F4 |
| medRxiv | `cei-data-source-medrxiv` | `medrxiv___search` | `query`, `max_results` | same | TBD | F4 |
| ChEMBL | `cei-data-source-chembl` | `chembl___search` | `query`, `max_results` | same | TBD | F4 |
| Open Targets | `cei-data-source-open-targets` | `opentargets___search` | `query`, `max_results` | same | TBD | F4 |
| CMS Coverage Database | `cei-data-source-cms-coverage` | `cmscoverage___search` | `query`, `max_results` | same | TBD | F4 |
| ICD-10 | `cei-data-source-icd10` | `icd10___search` | `query`, `max_results` | same | TBD | F4 |
| NPI Registry | `cei-data-source-npi-registry` | `npi___search` | `query`, `max_results` | same | TBD | F4 |
| PubChem | `cei-data-source-pubchem` | `pubchem___search` | `query`, `max_results` | same | TBD | F4 |

All ten return a `DataSourceResult` (`source_type`, `results: list[dict]`, `citations: list[Citation]`, `query_metadata`). The `Citation` model is fixed and shared across all sources: `source_id, source_type, url, title, authors, year, relevant_passage, retrieved_at`.

**Retry/backoff:** none implemented today (`_direct_invoke` methods call `requests.get(...)` once, `timeout=10`, no retry decorator). **Gap:** every Gateway Lambda needs retry with exponential backoff added before F4 ships (explicit requirement from B22: "Tool Lambdas need retry with backoff").

**Optional user API key (B20):** all ten sources currently work key-free. Target: a user can add an optional key (e.g., NCBI) for higher rate limits, read from Secrets Manager inside the Lambda using the caller's `sub` claim (see `auth-and-tool-scope.md` §5). Not implemented — no Lambda reads Secrets Manager today.

## 2. NCBI Gateway Targets (new, B27)

Originally added for the NCBI tool agent (BLAST, taxonomy, sequence search). **NCBI is not tool-agent only (B85):** specialist agents also get the NCBI scope, on the same footing as the other 10 sources — so specialists call these targets directly too, not only through a tool agent. None of this exists in the codebase yet.

| Target | Proposed MCP tool name(s) | Inputs/outputs | Citation fields | Upstream limits | Phase |
|---|---|---|---|---|---|
| NCBI Taxonomy | `ncbi-taxonomy___lineage` | taxon name/ID → lineage tree | source_id = taxon ID, url to NCBI Taxonomy Browser | shared NCBI E-utilities limit (3/10 rps, see §1) | F15 |
| NCBI Sequence/efetch | `ncbi-sequence___efetch` | accession/ID → FASTA/GenBank record | source_id = accession, url to NCBI record | same | F15 |
| NCBI BLAST | `ncbi-blast___submit`, `ncbi-blast___poll` | sequence + program/database → RID (submit), RID → results (poll) | source_id = RID, url to BLAST report | BLAST QBLAST API has its own courtesy limits (poll interval ≥ 60s recommended); **TBD** exact values | F15 |

BLAST is asynchronous by nature (submit → poll for a result ID); the tool agent's generic graph needs a submit/poll node pair or a wait loop — **Proposal (not decided):** whether this lives in the shared tool-agent graph as a generic async-tool pattern, or as a per-profile custom subgraph hook (see `agents.md` §5).

## 3. Life Sciences Operations Lambda (B42)

One Python Gateway target, 4 tool groups, no model call inside any of them (deterministic). Not implemented; likely needs a container image (RDKit is large).

| Group | Proposed MCP tool name(s) | Notes | Phase |
|---|---|---|---|
| Sequence basics | `lsops___validate_sequence`, `lsops___transcribe`, `lsops___reverse_complement`, `lsops___translate`, `lsops___orf_finder`, `lsops___gc_content`, `lsops___composition` | Biopython. IUPAC validation for DNA/RNA/protein; translate takes a frame (1–6) and codon table. | F12 |
| Protein/primer properties | `lsops___protein_properties` (MW, pI, extinction coefficient, GRAVY), `lsops___primer_tm`, `lsops___primer_hairpin_dimer`, `lsops___restriction_sites` | Biopython. | F12 |
| Chemistry | `lsops___validate_structure` (SMILES/InChI), `lsops___molecular_descriptors` (MW, LogP, TPSA, HBD/HBA), `lsops___lipinski_check`, `lsops___tanimoto_similarity` | RDKit. | F12 |
| Lab calculations/notation | `lsops___molarity`, `lsops___dilution` (C1V1=C2V2), `lsops___unit_convert`, `lsops___hgvs_check`, `lsops___parse_fasta_genbank` | No external API call; pure computation/parsing. | F12 |

No citation fields apply (deterministic compute, not a literature/database lookup). No upstream rate limit (runs entirely inside the Lambda). All agents (specialist, pipeline, tool agents) can call this target under the shared `cei-tools/common` scope, granted to every agent principal by default (B70; see `auth-and-tool-scope.md` §3–§4). This resolves the B42 open question in favor of a single shared scope, not a per-agent explicit grant.

## 4. AgentCore Web Search Connector (B44)

Built-in AgentCore connector, not a custom Lambda — but it **is** a Gateway target (B86): the Web Search connector is registered on the Gateway as a built-in connector target, so it is reachable through the same MCP path as the source Lambdas and is gated by a Cedar permit, not by IAM/runtime binding alone. `connectorId: "web-search"`, pinned version `v1.2.0`.

| Item | Value |
|---|---|
| Tool name | Whatever AgentCore's built-in binding exposes for connector `web-search` (not necessarily the `<target>___<tool>` naming used for custom Lambda targets, since this is a built-in connector target rather than a custom Lambda target — but it is still a Gateway target, B86) |
| Inputs | query string, optional domain include/exclude list, optional `publishedDateFilter` |
| Outputs | search results with URL, title, snippet |
| Citation fields | store only `url`, `title`, and the cited snippet — **never** the full result list (terms forbid bulk storage, B44) |
| Cost | $7 / 1,000 queries |
| Region availability | us-east-1, eu-west-1, ap-northeast-1 only; `deploy.sh` must disable web search outside these regions |
| Auth | Cedar Policy permit under the shared `cei-tools/common` scope (B70/B86), same enforcement pattern as the Life Sciences Ops and KB retrieval targets — see `.agents/specs/auth-and-tool-scope.md` §6; no per-user API key |
| Phase | F12 |

Not implemented (no code references a web search connector today).

## 5. AgentCore Code Interpreter (B39)

Exists (partial): `CodeInterpreterCustom` is defined in `backend/lib/backend-stack.ts` (public network mode), and `shared/tools/code_interpreter.py` (`CodeInterpreterTool`) already calls `start_code_interpreter_session` / `invoke_code_interpreter` / `stop_code_interpreter_session` against `bedrock-agentcore`.

| Item | Value |
|---|---|
| Tool name | `run_code` (LangChain `StructuredTool` name today). By decision, Code Interpreter is **never** a Gateway target (B86, not a "not yet" — a permanent design choice): it is invoked directly via the `bedrock-agentcore` boto3 client, not through the MCP Gateway, and so is not gated by a Cedar permit on a `GatewayAction`. Target: the runtime binding (IAM `grantUse` on the Code Interpreter resource) controls access; the `cei-tools/common` scope claim is still the token-level signal the runtime checks before it grants that access (B70), but the check happens outside the Gateway/Cedar engine. |
| Inputs | `code: str`, `language: python\|javascript\|typescript` |
| Outputs | stdout, stderr, exit_code, execution_time_ms, is_error, content[] |
| Used by | Drafting (`export_documents`, chart rendering, PDF/DOCX generation via `pdf_code`/`docx_code`) |
| Gap | CDK resource exists but is not yet granted (`grantUse`) to any Lambda or runtime role — see the TODO comment in `backend-stack.ts` |
| Phase | F1 (CDK exists), wiring to runtimes at F5+ |

Programmatic tool calling (PTC) through the Code Interpreter is a later-phase spike with a safety review (B39) — not scheduled in F1–F21.

## 6. Artifact / Presenters (B43)

Not a callable tool in the MCP sense — this is the deterministic presentation layer plus the one sandboxed model-authored exception.

| Piece | Status | Notes |
|---|---|---|
| Deterministic CEI UI presenters | exists (partial) | `shared/presenters/cei_ui.py`, `shared/presenters/markdown.py` build the allowlisted component graph from `ResponseDocument`; implemented for Research/Drafting outputs. |
| `Artifact` component (model-authored HTML in a sandboxed iframe) | exists (partial) | Schema + denylist lint exist per `docs/cei-ui-protocol.md`; production CSP/CDN interaction (residual risk noted there) is still open. |
| Phase | F6 (streaming UI phase) for the production wiring; the schema itself already exists. |

## 7. Knowledge Base Retrieval Tool (B33/B35/B77)

Planned. Bedrock KB backed by S3 Vectors. KB retrieval is a Gateway Lambda target, not an AgentCore Managed RAG built-in binding (B77 — resolves the earlier "spike needed" TBD). The Lambda adds the `user_id` + `session_id` filter itself, read from the caller's token claims; the agent cannot change the filter (B77, B31, B33).

| Item | Value |
|---|---|
| MCP tool name | `kb___retrieve` |
| Inputs | query text (the Lambda derives `user_id`/`session_id` from the token claims, not from agent input) |
| Outputs | ranked chunks with source `file_id`, page/offset, and the raw chunk text |
| Citation fields | `file_id`, and whatever page/offset metadata the KB parser preserves |
| Rate limit | Bedrock KB service quotas — TBD |
| Scope | Gated by the shared `cei-tools/common` scope, granted to every agent by default (B70) |
| Phase | S5 (spike), F10 (production) |

## 8. Export Tools (B41)

Exists (partial): `shared/tools/export.py` (`ExportTool`) already generates `md`, `txt`, `csv`, `latex` locally and `pdf`/`docx` via the Code Interpreter (`export_pdf.py`, `export_docx.py`, `export_latex.py`).

| Format | Status |
|---|---|
| Markdown | exists — local render, charts inlined as base64 PNG data URIs |
| TXT | exists — local render, markdown stripped |
| CSV | exists — extracts pipe-delimited tables from sections |
| LaTeX | exists — `latex_export()`, writes `.tex` + chart image files |
| PDF | exists (partial) — needs a live Code Interpreter session (`_generate_via_ci`) |
| DOCX | exists (partial) — same Code Interpreter dependency |
| Slides (PPTX) | deferred, optional (B41) — not implemented, not scheduled in F1–F21 |

Soft acknowledgment gate ("I have reviewed this for accuracy," D11) before export is not implemented anywhere in `export_documents` today — gap, phase F7.

## 9. TBD

- Upstream rate limits for ClinicalTrials.gov, bioRxiv, medRxiv, ChEMBL, Open Targets, CMS Coverage, ICD-10, NPI Registry, PubChem — not in the decision log; each Gateway Lambda's retry/backoff design needs these values confirmed against each provider's published limits before F4.
- NCBI BLAST poll interval and concurrent-submission limits — TBD.
- Tool-agent BLAST submit/poll: generic async-tool pattern in the shared graph vs. a per-profile custom subgraph hook — Proposal (not decided).
- Design of user-configured paid sources beyond the ten free ones (deferred by the product owner, per decision log "Still open").
- Exact Cedar action-name/tag syntax for the Web Search connector's Gateway target — the syntax in `auth-and-tool-scope.md` §6 is illustrative; S2 confirms (B86).

Resolved by B67–B86: KB retrieval tool is a Gateway Lambda target with a token-claim filter, not an AgentCore Managed RAG built-in binding (B77); Life Sciences Operations, Code Interpreter, Web Search, and KB retrieval all share one `cei-tools/common` scope granted to every agent by default (B70); specialist agents also get the NCBI scope, not just tool agents (B85); the Web Search connector is a Gateway built-in connector target gated by Cedar, while Code Interpreter is not a Gateway target at all (B86).
