# F04 — Data tools

Status: not started | Depends on: F1, S2 | Decisions: B19, B20, B22, B26, B27, B30, B67, B69, B70, B86 | Specs: [`../specs/tools.md`](../specs/tools.md) §1–2, [`../specs/auth-and-tool-scope.md`](../specs/auth-and-tool-scope.md) (full)

## Goal

Deploy the AgentCore Gateway with the 10 data source Lambdas as real MCP targets, gated by Cognito M2M scoped tokens and a Cedar Policy in `ENFORCE`, using S2's confirmed mechanism. After this phase, a scoped token can call exactly the source Lambdas its scope allows, and no others — ready for F5's Research agent to call for real.

## Scope — In / Out

**In:** the AgentCore Gateway, the 10 source Lambdas (ported from today's `_direct_invoke` HTTP logic to real Lambda handlers with retry/backoff), the Cognito resource server with the full 11-scope list (10 `cei-tools/src.<source>` scopes + the shared `cei-tools/common` scope, per B70/B86 — NCBI's own scope (`cei-tools/src.ncbi`) from B27 is F15 scope since NCBI targets themselves are F15), the Cognito Router M2M client, the pre-token-generation trigger, the Cedar policy set (`LOG_ONLY` → `ENFORCE`), and the token-refresh internal endpoint (B67).

**Out:** the NCBI Gateway targets themselves (BLAST/taxonomy/sequence — F15, though this phase should reserve the `cei-tools/src.ncbi` scope name so F15 does not have to touch the resource server again), the Life Sciences Operations Lambda (F12), the Web Search connector (F12), the real Session Router Lambda (F5/F9 build it; this phase can use a temporary test script in place of the Router to exercise the Gateway, per S2's pattern).

## Deliverables

- `backend/lib/backend-stack.ts` (modify) — add: the AgentCore `Gateway` construct with 10 Lambda targets, the Cedar `PolicyStore`/policy-set construct (default deny, `LOG_ONLY` initially), the Cognito resource server (`cei-tools`) with scopes `cei-tools/src.{pubmed,clinicaltrials,biorxiv,medrxiv,chembl,opentargets,cmscoverage,icd10,npi,pubchem}` plus `cei-tools/common` (B70/B86 shared scope, granted by default to every principal), and the Router M2M `UserPoolClient` (confidential, client-credentials) with IAM restricted so only the Router's execution role can read its Secrets Manager secret (per B30 §2, confirmed by S2).
- `backend/lambdas/data-source-pubmed/`, `.../data-source-clinicaltrials/`, `.../data-source-biorxiv/`, `.../data-source-medrxiv/`, `.../data-source-chembl/`, `.../data-source-opentargets/`, `.../data-source-cmscoverage/`, `.../data-source-icd10/`, `.../data-source-npi/`, `.../data-source-pubchem/` (new, Python) — each ports the corresponding class under `backend/agents/shared/tools/data_sources/*.py` into a Lambda handler, replaces `_direct_invoke` with the production HTTP call path, adds retry/exponential backoff (explicit gap named in `tools.md` §1), and adds the optional user-API-key Secrets Manager lookup (B20, secret name `cei/user-keys/{user_id}/{source_id}` per B78).
- `backend/lambdas/pre-token-generation/` (new, Python) — the V3 trigger adding `sub` and session ID custom claims (per `auth-and-tool-scope.md` §4).
- `backend/lambdas/token-refresh/` (new, Python) — the internal IAM-authed endpoint from B67: checks the run/job is still active (Job ID Store or an active-run record — F16 owns the real active-run record, so this phase's check can be a minimal stub against whatever exists at F4 time, revisited by F16), then reissues a same-scope token.
- `backend/agents/shared/config.py` (modify) — remove/gate `direct_mode`/`CEI_DIRECT_MODE` from any path that would run in production (per B19); keep it usable for local CLI development only.

## Tasks

- [ ] 1. Add the Cognito resource server and the 11 scopes (10 `cei-tools/src.<source>` scopes + shared `cei-tools/common`) to the `UserPool` created in F2, checking the substring-collision rule (B69's confirmed target-level granularity; verify with the same pairwise check `auth-and-tool-scope.md` §3 already ran, since this phase adds `cei-tools/common` and (reserved) `cei-tools/src.ncbi` to that list).
- [ ] 2. Add the Router M2M app client; write the IAM policy restricting `secretsmanager:GetSecretValue` on its secret to only the Router Lambda's execution role (the real Router does not exist until F5 — grant this to a placeholder role now and re-point it in F5, noted as a handoff item below).
- [ ] 3. Write `pre-token-generation`: V3 trigger, adds `sub` and session ID (passed in via `aws_client_metadata` when the Router requests a token) as custom claims.
- [ ] 4. Port each of the 10 `DataSourceTool` subclasses (`backend/agents/shared/tools/data_sources/*.py`) into its own Lambda handler; keep the existing `_build_payload()` request/response shape, replace the local-dev `_direct_invoke` call path with the production HTTP call, and add retry with exponential backoff (this is an explicit, named gap in `tools.md` §1 — do not ship without it).
- [ ] 5. Add the optional user-API-key lookup to each Lambda: read `sub` from the forwarded token, look up `cei/user-keys/{user_id}/{source_id}` in Secrets Manager, fall back to the free/unauthenticated call path if no key exists (B20).
- [ ] 6. Deploy the AgentCore Gateway with all 10 Lambdas as targets, using the MCP tool naming convention `<target>___<tool>` (e.g. `pubmed___search`) per `tools.md`'s proposal.
- [ ] 7. Write the Cedar policy set: default deny, `initialize`/`tools/list` permitted unconditionally, one permit per source target (target-level granularity, B69), plus the shared `cei-tools/common` scope permit granted-by-default pattern (B70/B86) reusing S2's confirmed approach. Start `LOG_ONLY`.
- [ ] 8. Run a `LOG_ONLY` audit period (use a test script standing in for the Router, per S2's pattern) and confirm no unexpected denies before flipping to `ENFORCE`.
- [ ] 9. Flip the policy set to `ENFORCE`.
- [ ] 10. Write `token-refresh`: IAM-authed Lambda (or API Gateway IAM-auth route), checks the calling run is still active, reissues a same-scope token without ever exposing the M2M secret to the caller (per B67).
- [ ] 11. Gate `CEI_DIRECT_MODE` out of any Lambda code path (`shared/config.py`, `shared/tools/base.py`) so production Lambdas never take the direct-HTTP shortcut; keep it available for local CLI runs only.

## Acceptance criteria

- All 10 source Lambdas are reachable as Gateway targets and return `DataSourceResult`-shaped output (matching today's `_build_payload`/`Citation` schema) unchanged in shape.
- A token scoped to only `cei-tools/src.pubmed` can call `pubmed___search` and is denied every other source target under `ENFORCE`; `tools/list` for that token shows only PubMed's tools.
- A token scoped to all 10 source scopes (simulating a specialist agent) can call every source target.
- Every principal, regardless of its explicit scopes, can reach the shared `cei-tools/common` scope's permit rule (mechanism only — the LS Ops Lambda itself does not exist until F12).
- A Lambda whose caller has a configured API key uses it; a Lambda whose caller has none falls back to the free path without erroring.
- The Router M2M client secret is not readable by any role other than its designated execution role.
- `token-refresh` reissues a token with the same scopes for an active run and refuses for an inactive one.
- `npx cdk synth` passes cdk-nag with ADR-format suppressions only.

## Tests

- Python unit tests per Lambda (pytest, extending `backend/agents/tests/` patterns): request/response shape matches today's `DataSourceResult`/`Citation` contract, retry/backoff triggers on a simulated upstream failure, API-key lookup falls back correctly when no secret exists.
- CDK assertion tests: Gateway has all 10 targets, Cognito resource server has exactly the expected scope set (fail the test if a new scope breaks the substring-collision rule), Router M2M client has no implicit broad grant.
- Integration test (scripted, reusing S2's test-harness pattern): scoped-token call succeeds/fails as expected against the real deployed Gateway under `ENFORCE`.
- `token-refresh` unit tests: active run → new token with same scopes; inactive/unknown run → refusal.

## Risks and open items

- Upstream rate limits for ClinicalTrials.gov, bioRxiv, medRxiv, ChEMBL, Open Targets, CMS Coverage, ICD-10, NPI Registry, and PubChem are not in the decision log (`tools.md` §9 TBD) — confirm each provider's published limits before finalizing each Lambda's retry/backoff parameters; do not guess a number and ship it silently.
- Denied-call behavior inside LangGraph (what an agent does when the Gateway returns a Policy deny) is still open per `auth-and-tool-scope.md` §8/§13 even after S2's spike characterizes the error shape — this phase's Lambdas and Gateway should surface a clear, consistent error, but the *agent-side* handling decision belongs to F5, not this phase.
- The Router M2M client's IAM restriction (task 2) is set up against a placeholder role since the real Router Lambda does not exist until F5 — flag this clearly so F5 re-points the IAM policy to the real Router role rather than leaving a stale placeholder grant in place.

## Handoff notes

- F5 (Research agent live) is the first real caller of this Gateway: it builds the Session Router Lambda that holds the M2M client secret this phase provisions, and must have its execution role substituted into the IAM restriction from task 2.
- F12 (Web search + LS ops) adds the real Life Sciences Operations Lambda and the Web Search connector target behind the `cei-tools/common` scope this phase already grants by default — F12 does not need to touch the Cognito resource server again. Note B86: Web Search is a Gateway built-in connector target (Cedar-controlled under `cei-tools/common`), but the Code Interpreter is *not* a Gateway target — its access is controlled by the AgentCore Runtime binding, not by a Cedar scope.
- F10 (Attachments + RAG) adds the KB retrieval Gateway target under this same `cei-tools/common` scope (B70/B86) — no new Cognito scope needed.
- F15 (Tool agents) adds the NCBI Gateway targets and needs the `cei-tools/src.ncbi` scope this phase should reserve (name-check it against the substring rule now, even if the target itself isn't built until F15). Once F15 ships, per B85 every specialist-agent token request (this phase's Router-issued tokens) should also request `cei-tools/src.ncbi`, so specialist agents get NCBI access like every other source.
- F18 (User API keys) adds the real Settings-page-driven Secrets Manager write path (`PUT/DELETE /settings/api-keys/{sourceId}`); this phase's Lambdas already assume that secret naming convention (`cei/user-keys/{user_id}/{source_id}`, B78) exists, so F18 only needs to add the write side.
