# Auth and Tool Scope

Status: planned. Source of truth: `.agents/decision-log.md`. See `architecture.md` for where Cognito and the Gateway sit in the request flows, `tools.md` for the target list each scope gates, and `security.md` (sibling spec, written in parallel) for the broader threat model — referenced here by name only.

None of this is built yet. The current repository has no Cognito resources, no Gateway, and no Policy engine (`backend/lib/backend-stack.ts` defines only the Code Interpreter).

## 1. Cognito User Pool

| Setting | Value | Decision |
|---|---|---|
| Sign-up | Self sign-up, email verification | B13 |
| MFA | None | B14 |
| Domain allowlist | Pre-sign-up Lambda trigger checks the email domain against an allowlist set by `deploy.sh` | B16 |
| Isolation | Every record partitioned by Cognito `sub`; no sharing, no teams | B12, D15 (superseded wording) |

**Pre-sign-up trigger:** rejects sign-up when the email domain is not on the allowlist. Allowlist values come from `deploy.sh` configuration (B47), stored as an environment variable or Lambda config, not hardcoded.

## 2. App Clients

| Client | Type | Used by | Notes |
|---|---|---|---|
| SPA client | Public client, Authorization Code + PKCE | founder-ai-dashboard (browser) | No client secret; standard SPA OAuth flow. |
| Router M2M client | Confidential client, client-credentials grant | Session Router Lambda and the Token Refresh Lambda only | **Only** these two Lambdas hold this secret (B30, B67). Both request a token with only the scopes the current request/run needs, per call. |

**IAM boundary (explicit prototype item, B30):** the AgentCore runtime execution role must **not** be able to read the M2M client secret from Secrets Manager. Only the Router Lambda's and the Token Refresh Lambda's execution roles get `secretsmanager:GetSecretValue` on that secret. This needs a concrete IAM policy test before F4 ships (listed as a prototype item in B30).

## 3. Resource Server and Scopes

One Cognito resource server, e.g. identifier `cei-tools`. One scope per Gateway source target, so specialist agents (which get every scope) and tool agents (which get exactly one scope) can both be expressed as scope sets.

**Collision rule (research fact):** Policy in AgentCore's Cedar `like` operator is a **substring** match, not an exact match. Scope/target names must be chosen so no name is a substring of another (the example given in the task brief: `cei-tools/src.ncbi` is fine only if no other scope name contains `ncbi` as a substring, and `ncbi` itself is not a substring of any other scope).

**Scope list (naming is a proposal; the per-source/shared split is decided by B69/B70):**

| Scope | Gathers access to |
|---|---|
| `cei-tools/src.pubmed` | PubMed target |
| `cei-tools/src.clinicaltrials` | ClinicalTrials.gov target |
| `cei-tools/src.biorxiv` | bioRxiv target |
| `cei-tools/src.medrxiv` | medRxiv target |
| `cei-tools/src.chembl` | ChEMBL target |
| `cei-tools/src.opentargets` | Open Targets target |
| `cei-tools/src.cmscoverage` | CMS Coverage target |
| `cei-tools/src.icd10` | ICD-10 target |
| `cei-tools/src.npi` | NPI Registry target |
| `cei-tools/src.pubchem` | PubChem target |
| `cei-tools/src.ncbi` | NCBI targets (taxonomy, sequence/efetch, BLAST) — originally tool-agent only (B27); specialist agents also get this scope, on the same footing as the other 10 sources (B85) |
| `cei-tools/common` | Life Sciences Operations, Code Interpreter, Web Search connector, and KB retrieval (B70) — shared, cross-cutting, not per-source |

Checked pairwise for the substring rule above: none of `pubmed, clinicaltrials, biorxiv, medrxiv, chembl, opentargets, cmscoverage, icd10, npi, pubchem, ncbi, common` is a substring of another in this list. If a future source name breaks this (e.g., a hypothetical `chem` scope substring-matching `chembl`), the naming must be revisited — flag any new scope addition for this check.

**Shared tools scope (B70 — resolves the earlier LS Ops/Code Interpreter Proposal):** one scope, `cei-tools/common`, for Life Sciences Operations (B42), Code Interpreter (B39), the Web Search connector (B44), and KB retrieval (B33/B77). Every agent and tool agent principal always gets this scope by default — no per-agent-type policy decision needed. See `tools.md` §3–§7.

**Specialist agents** (Research, Drafting, Analysis, Pipeline Manager) get **all** source scopes — the 10 sources plus NCBI, 11 total (B85) — plus `cei-tools/common` (B30, B70) — they call the Gateway directly (B26), never through a tool agent.

**Tool agents** get exactly **one** source scope, matching the profile loaded for the current request, plus `cei-tools/common` (B21, B29, B30, B70).

## 4. Pre-Token-Generation Trigger

A Pre-Token-Generation Lambda trigger (V3 event, so it can read `aws_client_metadata` on the token request) adds two custom claims to every issued token (B30):

- the user's Cognito `sub`
- the current session ID

The Session Router passes these through `aws_client_metadata` when it requests a scoped M2M token on the user's behalf, so the Gateway Lambdas can later read the user `sub` claim straight from the token (see §5) without a separate lookup.

## 5. Token Flow: Router → Runtime → Gateway

```
User request (Cognito user token, SPA session)
  → Session Router Lambda (holds the M2M client secret)
    → requests a client-credentials token scoped to ONLY the scopes this request needs
      (all source scopes for a specialist agent; exactly one source scope for a tool agent)
      with user sub + session id injected via aws_client_metadata (pre-token-generation trigger, §4)
  → Router passes that token to the AgentCore Runtime invocation
    (as an allowlisted header, or as the Authorization header — TBD, see §8)
  → the agent forwards the token to the AgentCore Gateway UNCHANGED
  → Gateway checks Policy (Cedar, §6) against the token's scope claims
  → the target Lambda reads the user `sub` claim from the token to look up
    that user's optional API key in Secrets Manager (B20)
```

The agent code itself never sees or modifies the token's scope claims; it only relays what the Router issued.

**Token refresh mid-run (B67 — resolves the token-TTL open item in §8):** tool tokens live 1 h. Before its token expires, a running agent (real-time or async) calls an internal token-refresh endpoint, `POST /internal/token-refresh` (IAM auth, not the Cognito authorizer — see `api.md`). The endpoint checks that the run/job is still active (the Job ID Store for background jobs, or the active-run record for real-time runs — see `data-model.md`) and, if so, returns a new token with the same scopes and the same user `sub`/session ID. The agent never receives, and never needs, the M2M client secret; only the Token Refresh Lambda holds the credential needed to mint tokens on the Router's behalf.

## 6. Cedar Policy Examples (Policy in AgentCore, GA 2026-03-03)

Principal type: `AgentCore::OAuthUser`, with JWT claims available as Cedar entity tags.

**Default deny, LOG_ONLY at first, then ENFORCE (B30):**

```
// Start every environment in LOG_ONLY. Only promote to ENFORCE after a
// clean audit period with no unexpected denies.
forbid(
    principal,
    action,
    resource
);
```

**Permit a source's tool calls when the token carries that source's scope (B69 — one Cedar permit per source target, covering all of that target's tools, not one permit per tool):**

```
permit(
    principal,
    action like GatewayAction::"pubmed___*",
    resource
)
when {
    principal.scope.contains("cei-tools/src.pubmed")
};
```
(Repeat per source scope in the table in §3 — one permit per target, matching every `pubmed___*`, `clinicaltrials___*`, etc. action for that target.)

**Permit the shared cross-cutting tools when the token carries the common scope (B70):**

```
permit(
    principal,
    action like GatewayAction::"lsops___*",
    resource
)
when {
    principal.scope.contains("cei-tools/common")
};

permit(
    principal,
    action == GatewayAction::"kb___retrieve",
    resource
)
when {
    principal.scope.contains("cei-tools/common")
};
```
**The Web Search connector is a Gateway built-in connector target (B86), so it IS gated by a Cedar permit like the other `cei-tools/common` targets above:**

```
permit(
    principal,
    action == GatewayAction::"web-search___search",
    resource
)
when {
    principal.scope.contains("cei-tools/common")
};
```

(Action name illustrative — the exact action-name string AgentCore's built-in `web-search` connector registers on the Gateway needs confirming in S2, per B86.)

Code Interpreter, by contrast, is **not** a Gateway target at all (B86) — it is invoked directly against the `bedrock-agentcore` runtime API, so it is gated by IAM/runtime binding (`grantUse`), not by a Cedar permit on a `GatewayAction` — see `tools.md` §5. The `cei-tools/common` scope claim is still the token-level signal the runtime checks before granting Code Interpreter access, but that check happens outside the Gateway/Cedar engine. All Cedar syntax in this section is illustrative; S2 confirms the exact tag/claim/action-name syntax against the GA Policy engine (B86).

**Permit `initialize` and `tools/list` unconditionally (needed for MCP handshake), but filter what `tools/list` actually returns to the caller's scopes:**

```
permit(
    principal,
    action in [GatewayAction::"initialize", GatewayAction::"tools/list"],
    resource
);
```
The Gateway's `tools/list` response filtering by scope tag (so a tool-agent with only `cei-tools/src.pubmed` never even sees the other nine tools listed) is an explicit prototype item in B30 — not yet verified against the GA Policy engine.

## 7. What Gets All Scopes vs. One Scope

| Principal | Scopes | Source |
|---|---|---|
| Research, Drafting, Analysis, Pipeline Manager agents | All 11 source scopes (10 sources + NCBI) + `cei-tools/common` | B30, B26, B70, B85 |
| Tool agent (per profile) | Exactly one source scope matching the loaded YAML profile + `cei-tools/common` | B29, B30, B70 |
| Session Router M2M client / Token Refresh Lambda | Requests any subset per-call; never holds a standing broad grant itself | B30, B67 |

## 8. Open Prototype Items (from B30, explicit in decision log)

- `tools/list` filtering by scope tags — needs a working test against GA Policy in AgentCore.
- Denied-call error shape inside LangGraph: what the agent sees and does when the Gateway returns a Policy deny (retry with a different tool? surface to the user? fail the turn?). **TBD.**
- Confirming the runtime execution role truly cannot read the M2M secret (IAM boundary test, §2).

Token TTL and refresh mid-run is resolved by B67 — see §5.

## 9. REST API Gateway Authorizer (B2)

One Cognito User Pools authorizer on the single Regional REST API, applied to `/chat/stream` and `/tools/{source}/chat/stream` (both STREAM integrations, B84) and all BUFFERED REST routes. Per the research facts, the Cognito authorizer on the streaming routes needs early, explicit testing — streaming integrations are less common and less documented than buffered ones.

## 10. Per-User Isolation (B12)

Every DynamoDB item, S3 object key, and Secrets Manager secret name is partitioned by the Cognito `sub`. No cross-user reads. This applies to: Session Metadata Store, Chat History Store, Attachment Metadata Store, User Metadata Table, API Key Store, and KB chunk metadata filters (`user_id`, `session_id`, `file_id` — B33). Exact key schemas belong to `data-model.md` (sibling spec).

## 11. Throttling and Concurrency (B22)

| Limit | Value |
|---|---|
| Per-user request rate | 20 requests/second |
| Per-user concurrent real-time (SSE) runs | 2 |
| Per-user concurrent background (async) runs | 1 |
| Agent-to-tool calls | Not throttled by us; upstream API limits still apply (e.g., NCBI 3 rps without a key, 10 rps with one) |

These limits are enforced at the API Gateway (request rate) and at the Session Router / Session Metadata Store (concurrency count check before dispatch) — exact enforcement point is implementation detail for `api.md`.

## 12. Gateway Lambda Access to User API Keys (B20)

A Gateway Lambda target reads the user's `sub` from the forwarded token's claims (see §5), then does a Secrets Manager lookup under a per-user secret name (prefix `cei/user-keys/...`, matching `shared/config.py`'s existing `api_key_secret_prefix` setting) to find an optional, user-provided key for that source (e.g., an NCBI API key for higher rate limits). If no key is found, the Lambda falls back to the free, unauthenticated call path that all ten sources already support today.

## 13. TBD

- Denied-call behavior inside LangGraph (§8) — no rule decided.
- Whether the Router→Runtime token travels as an allowlisted custom header or as `Authorization` (§5) — Proposal (not decided), depends on `api.md`/`streaming-protocol.md`.
- `tools/list` scope-filtering behavior — needs a working Policy-in-AgentCore prototype (§8, B30).

Resolved by B67–B86: token TTL and refresh mid-run (B67, §5), Cedar permit granularity is target-level (B69, §6), and the LS Ops/Code Interpreter/Web Search/KB retrieval scope model is one shared `cei-tools/common` scope granted by default (B70, §3, §7); specialist agents also get the NCBI scope, not tool-agent only (B85, §3, §7); the Web Search connector is a Cedar-gated Gateway target while Code Interpreter is not a Gateway target at all (B86, §6).
