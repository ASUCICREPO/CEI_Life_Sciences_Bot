# F12 — Web search + LS Ops
Status: not started | Depends on: F4 | Decisions: B40, B42, B44, B70, B86 | Specs: ../specs/tools.md, ../specs/architecture.md, ../specs/auth-and-tool-scope.md

## Goal
Give every agent two more tools: a built-in AgentCore web search connector for general research, and a deterministic Life Sciences Operations (LS Ops) Lambda for common life-sciences calculations (sequence, protein/primer, chemistry, lab math) that need no model call.

## Scope — In / Out

**In**
- One AgentCore Web Search connector binding, `connectorId: "web-search"`, pinned `v1.2.0` (B44).
- Region gate: web search is enabled only in `us-east-1`, `eu-west-1`, `ap-northeast-1`; `deploy.sh` disables it elsewhere (B44, B45).
- Citation storage rule: only `url`, `title`, and the cited snippet are ever stored — never the full result list (B44).
- One Life Sciences Operations Lambda, one Gateway target, 4 tool groups exactly as listed in B42 (sequence basics; protein/primer properties; chemistry via RDKit; lab calculations/notation).
- LS Ops and Web Search both granted under the shared `cei-tools/common` scope (B70) — every agent and tool agent gets this scope by default. Per B86, the Web Search connector is a Gateway built-in connector target, so Cedar controls it under `cei-tools/common` like LS Ops; the Code Interpreter is explicitly *not* a Gateway target, so it is never part of this Cedar scope — its access is controlled by the AgentCore Runtime binding (`codeInterpreter.grantUse`), unrelated to this phase's Cognito/Cedar work.

**Out**
- Web page fetching beyond the connector's own snippets (B44 explicitly forbids this).
- Paid/user-configured data sources beyond the ten free sources (deferred, see `later.md`).
- Programmatic tool calling (PTC) through the Code Interpreter (deferred, see `later.md`).
- NCBI-specific Gateway targets (taxonomy, sequence, BLAST) — those belong to F15.

## Deliverables
- `backend/lambdas/lsops/handler.py` (new) — dispatches to the 4 tool groups.
- `backend/lambdas/lsops/sequence_basics.py`, `protein_primer.py`, `chemistry.py`, `lab_calculations.py` (new) — Biopython-backed (sequence/protein/primer) and RDKit-backed (chemistry) implementations, matching the tool names in `tools.md` §3 exactly (`lsops___validate_sequence`, `lsops___transcribe`, `lsops___reverse_complement`, `lsops___translate`, `lsops___orf_finder`, `lsops___gc_content`, `lsops___composition`, `lsops___protein_properties`, `lsops___primer_tm`, `lsops___primer_hairpin_dimer`, `lsops___restriction_sites`, `lsops___validate_structure`, `lsops___molecular_descriptors`, `lsops___lipinski_check`, `lsops___tanimoto_similarity`, `lsops___molarity`, `lsops___dilution`, `lsops___unit_convert`, `lsops___hgvs_check`, `lsops___parse_fasta_genbank`).
- `backend/lambdas/lsops/Dockerfile` (new) — container image; RDKit is large enough that a zip-based Lambda is unlikely to fit (B42 note).
- `backend/lib/backend-stack.ts` — add:
  - the LS Ops Lambda (container image) and its Gateway target,
  - the Web Search connector binding, gated by a CDK context/env value for Region (B45), with `deploy.sh` (from F1) responsible for turning the context value off outside the 3 supported Regions,
  - the `cei-tools/common` scope grant on both targets (coordinate with F4's Cedar setup; if F4 has not yet defined the Cedar policy file, add a TODO comment matching the existing `backend-stack.ts` style, as seen for `codeInterpreter.grantUse`).
- `backend/agents/shared/tools/lsops_client.py` (new) — a thin `StructuredTool`-producing wrapper, following the same shape as `shared/tools/code_interpreter.py`, so agent graphs can bind it like any other LangChain tool during local/CLI runs before the Gateway exists.
- `backend/agents/shared/schemas/citation.py` — no change expected; confirm the Web Search connector's output maps onto the existing `Citation` model (`url`, `title`) with no NCBI-style `source_id`; if it does not fit cleanly, add an optional `Citation.snippet` field here.

## Tasks
- [ ] 1. Implement `lsops___validate_sequence`, `transcribe`, `reverse_complement`, `translate` (6 frames, codon tables), `orf_finder`, `gc_content`, `composition` using Biopython.
- [ ] 2. Implement `lsops___protein_properties` (MW, pI, extinction coefficient, GRAVY), `primer_tm`, `primer_hairpin_dimer`, `restriction_sites` using Biopython.
- [ ] 3. Implement `lsops___validate_structure` (SMILES/InChI), `molecular_descriptors` (MW, LogP, TPSA, HBD/HBA), `lipinski_check`, `tanimoto_similarity` using RDKit.
- [ ] 4. Implement `lsops___molarity`, `dilution` (C1V1=C2V2), `unit_convert`, `hgvs_check`, `parse_fasta_genbank` — pure computation/parsing, no external call.
- [ ] 5. Build the container image (Dockerfile) and confirm cold-start time is acceptable for a synchronous tool call.
- [ ] 6. Add the LS Ops Gateway target and `cei-tools/common` scope grant in `backend-stack.ts`.
- [ ] 7. Add the Web Search connector binding with a Region-conditional CDK construct.
- [ ] 8. Update `deploy.sh` (from F1) to read the target Region and set the web-search-enabled context flag automatically.
- [ ] 9. Implement the citation storage rule at the point where a Web Search tool result is turned into a trace event (coordinate with F6's `trace_event` implementation — store only `url`/`title`/cited snippet, never the raw result list).
- [ ] 10. Add the local `lsops_client.py` LangChain tool wrapper so Research/Drafting/Analysis can bind LS Ops during local CLI runs the same way they already bind the Code Interpreter.

## Acceptance criteria
- Each of the 19 LS Ops tool names above returns a deterministic result for a known input (for example, `translate` on a known ORF returns the known protein sequence) and never calls a model.
- Deploying outside `us-east-1`/`eu-west-1`/`ap-northeast-1` results in the Web Search connector not being provisioned, with no error.
- A trace entry for a web search tool call contains only `url`, `title`, and the snippet actually cited — verified by a unit test that fails if the full result list is present.
- The LS Ops Lambda is reachable under the `cei-tools/common` scope by both a specialist agent token and a tool-agent token (once F4's Cedar policy exists).

## Tests
- Unit (pytest): one test per LS Ops tool group against known reference values (for example, a known primer Tm, a known Lipinski Rule-of-5 pass/fail compound).
- Unit: citation-trimming logic for Web Search results (full-result-list input → trimmed trace output).
- CDK assertion tests: LS Ops Lambda is a container image function; Gateway target exists; Web Search connector construct is present/absent based on the Region context value; cdk-nag passes or carries an ADR-format suppression for the container image size / public network need.
- Integration: a local LangGraph run using `lsops_client.py` calls `lsops___dilution` and gets the correct answer, exercising the same code path the Gateway will use later.

## Risks and open items
- Web Search deployment remains open: the AgentCore API supports `source.version`, but the current CloudFormation `AWS::BedrockAgentCore::GatewayTarget ConnectorSource` schema only exposes `ConnectorId`. Do not deploy an unpinned default in place of B44's approved 1.2.0; implement and test a lifecycle-managed API-backed resource or wait for CloudFormation version support. AWS references: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-add-target-api-target-config.html and https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-properties-bedrockagentcore-gatewaytarget-connectorsource.html.
- Resolved: B70 states plainly that "all agents and tool agents always get" the shared `cei-tools/common` scope, and B86 confirms the Code Interpreter is not a Gateway target at all (so it is outside this Cedar scope question entirely — it is controlled by the runtime binding). This phase implements the granted-by-default reading for LS Ops and Web Search; no further decision is needed. If `auth-and-tool-scope.md` still shows this as open when you read it, follow B70/B86, not the older spec text — the parallel spec-update agent may not have applied them yet.
- Upstream rate limits are not a concern here (LS Ops has no external API calls), but the Web Search connector's $7/1,000-query cost is an unbudgeted MVP cost (B16 has no token/cost budget) — accepted gap, not a blocker.
- RDKit container image size/cold start is a build risk; if it is too slow, revisit as a Lambda-behind-Fargate-like pattern — but note B25 already removed Fargate from the architecture, so any such change needs a new decision, not an assumption.

## Handoff notes
- F15 (Tool agents) reuses the `cei-tools/common` scope pattern established here for its own profiles.
- F14 (Analysis agent) and F17 (Pipeline Manager) bind `lsops_client.py` and the Web Search tool the same way Research/Drafting do once this phase ships.
- F20 (Alarms) should consider a guardrail/cost alarm candidate for Web Search query volume, though B75's fixed alarm list does not currently include one — flagged for F20 to decide, not decided here.
