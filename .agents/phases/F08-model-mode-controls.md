# F08 — Model + mode controls

Status: not started | Depends on: F6 | Decisions: B18, B51, B51a, B55, B76, D16 (kept, per B61) | Specs: [`../specs/agents.md`](../specs/agents.md) §6 (Cross-Agent Mechanisms), [`../specs/api.md`](../specs/api.md) (`/models`, `/settings`), [`../specs/security.md`](../specs/security.md) (Model calls)

Implementation note: Research reads the saved chat mode and forwards its resolved
model through its graph. Until account model discovery and the global blocklist are
wired, Research accepts only its configured runtime default and rejects a different
saved selection instead of silently ignoring it. The runtime currently uses the
default in `AgentConfig`: the deployment configuration is not yet passed as
`CEI_DEFAULT_MODEL_ID`. This temporary restriction is not the approved final model
selection behavior. Drafting, title-model policy, Guardrails, discovery, settings,
and prompt caching remain incomplete; no deployed behavior has been verified.

## Goal

Let a user pick any (non-blocklisted) Bedrock model per chat, set science/business mode per chat (changeable mid-chat), and make every agent model call go through Bedrock Guardrails with input-only PII/PHI masking and prompt caching on the static system prompt and tool definitions.

## Scope — In / Out

**In:** `GET /models` (blocklist-filtered model list), `GET/PUT /settings` (`default_model_id`, `default_mode`), the `PATCH /sessions/{id}` mode-change path (already routed in F3, this phase makes `mode` actually affect the agent's prompt selection), wiring `get_model(model_id=...)` calls to the user's selection at every agent call site, the env-var model blocklist enforcement, Bedrock Guardrails on every model call, and prompt caching (`cachePoint` on system prompt + tool defs).

**Out:** per-user model restriction beyond the global blocklist (B76 explicitly decides there is none — do not build a per-user override mechanism), the User Metadata Table's other fields not related to model/mode (F3/F18 own those), any new agent (Analysis/Pipeline Manager model wiring happens when those agents are built in F14/F17, reusing this phase's mechanism).

## Deliverables

- `backend/agents/shared/utils.py` (modify) — `get_model()` adds a `cachePoint` on the system prompt and tool definitions (D16, kept per B61); confirm conversation history is excluded from the cache point per D16's original intent.
- `backend/agents/shared/config.py` (modify) — add `model_blocklist: list[str]` read from the env var `deploy.sh` sets (per F1's config loader), and a helper to filter a model list against it.
- `backend/agents/research/nodes.py`, `drafting/nodes.py` (modify) — every `get_model()` call site passes the session's `model_id` (from `ChatMetadataRecord.model_id`, F3) instead of relying on the fixed `default_model_id`.
- `backend/agents/shared/prompts/loader.py` (modify, if needed) — confirm `PromptLoader` already switches science/business variants correctly per `mode`; wire the session's live `mode` value (which can change mid-chat per B55) into each turn's prompt selection, not just at session creation.
- `backend/lambdas/models-settings-handler/` (new, Python) — `GET /models` (blocklist-filtered), `GET /settings`, `PUT /settings`.
- Guardrails configuration (new, in `backend-stack.ts` or a Bedrock Guardrail CDK construct) — one Guardrail applied to every agent model call, input PII/PHI masking only (B51a), never exposed in the trace (B7, already enforced by F6's `trace_event` never-show list — confirm Guardrail config specifically is covered).

## Tasks

- [ ] 1. Add `cachePoint` support to `get_model()`/`ChatBedrockConverse` construction in `shared/utils.py`, caching the system prompt and tool definitions only.
- [ ] 2. Add `model_blocklist` to `shared/config.py`, sourced from the env var F1's `deploy.sh` config sets; write the filter helper used by both `GET /models` and any server-side validation that rejects a blocklisted `model_id` on session create/update.
- [ ] 3. Update every agent call site (`research/nodes.py`, `drafting/nodes.py`) to pass the session's `model_id` into `get_model()`.
- [ ] 4. Wire live `mode` (science/business, changeable mid-chat per B55) into the per-turn prompt selection — confirm a mode change via `PATCH /sessions/{id}` takes effect on the *next* turn without requiring a new session.
- [ ] 5. Create the Bedrock Guardrail (CDK construct or manual + referenced ID, per whatever AgentCore/Bedrock CDK support exists at implementation time) with a PII/PHI filter mode — flag the exact filter strictness as **Proposal (not decided)** per `security.md`'s open item, and pick a reasonable default (input-masking only, per B51a) rather than leaving Guardrails off entirely.
- [ ] 6. Apply the Guardrail to every agent model call (Research, Drafting, and any agent live at this point) — confirm tool results and model output are *not* filtered (B51a: public NPI/author data must survive unfiltered).
- [ ] 7. Write `models-settings-handler`: `GET /models` (blocklist-filtered list), `GET /settings` / `PUT /settings` for `default_model_id`/`default_mode` on the User Metadata Table (first real use of that table beyond a config reference, per `data-model.md` §4 — implement `get`/`put` repository methods there, since none exist yet).
- [ ] 8. Confirm the trace/SSE redaction rule (B7, enforced in F6) specifically never surfaces the Guardrail's own configuration, even indirectly (e.g., through an error message naming a Guardrail ID).

## Acceptance criteria

- `GET /models` never returns a blocklisted model ID, even if the underlying Bedrock account has it enabled.
- A session created with a specific `model_id` uses that model for every turn's agent calls; a session with no explicit selection uses `default_model_id` from `UserMetadataTable`, falling back to the global default if the user has none set.
- Changing a chat's `mode` via `PATCH /sessions/{id}` changes the system prompt variant used starting with the next turn.
- Every agent model call passes through the Guardrail; a PII/PHI value in user input is masked before reaching the model; a public NPI/author name in a tool result or model output is *not* masked.
- No trace/error surface ever reveals the model ID or Guardrail configuration to the client, per B7.
- Prompt caching's `cachePoint` is present on the system prompt/tool-def portion of the request and absent from the conversation-history portion.

## Tests

- `pytest`: `get_model()` cache-point placement (assert the request payload structure), `model_blocklist` filtering logic, per-session `model_id`/`mode` passthrough in `research/nodes.py`/`drafting/nodes.py` (mocked model calls).
- `pytest`/scripted: Guardrail masks a synthetic PII string in user input, does not mask the same string pattern appearing in a tool-result fixture.
- `pytest` for `models-settings-handler`: blocklist filtering on `GET /models`, settings get/put round-trip, rejection of a `PUT /settings` request setting `default_model_id` to a blocklisted value.
- Redaction test (extend F6's task-11 test): assert no Guardrail ID/config ever appears in a `trace_event` or error payload.

## Risks and open items

- Guardrail PII filter mode/strictness is explicitly undecided in the decision log and in `security.md`'s open items — this phase must ship *some* working default (per B51a's scope: input-only masking) but should flag the exact strictness setting as a placeholder pending user review, not present it as final.
- B76 is explicit that there is no per-user model restriction beyond the global blocklist — do not build `blocked_model_overrides` (the `data-model.md` §4 proposal that predates B76); if you see that field proposed in the spec text, it is superseded by B76.
- Prompt caching's real effectiveness (cache hit rate, cost impact) is not measured by any test in this phase — that is an operational concern for F20/F21, not a blocking acceptance criterion here.

## Handoff notes

- F14 (Analysis agent) and F17 (Pipeline Manager), once built, must call `get_model()` the same way this phase establishes for Research/Drafting — no new mechanism needed, just the same call-site pattern.
- F18 (User API keys) extends the same `UserMetadataTable` this phase starts using for `default_model_id`/`default_mode` — coordinate on the repository module (`shared/persistence/` — note if a `user_metadata.py` module is added here, so F18 extends it rather than duplicating it).
- F21 (Hardening) should specifically re-review the Guardrail PII filter mode this phase ships as a placeholder default, since it is an explicitly open security item.
