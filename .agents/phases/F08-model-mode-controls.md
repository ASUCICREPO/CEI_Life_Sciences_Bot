# F08 — Model + mode controls

Status: partial (blocklist, Guardrail on every `get_model()`, model discovery, `GET /models`, `GET`/`PUT /settings`, UserMetadataTable, model picker, Settings defaults, any-Bedrock-model selection with widened inference IAM, and prompt caching built in code; saved defaults not applied to the implicit session-creation path yet; not verified on AWS; see "Implementation status") | Depends on: F6 | Decisions: B18, B51, B51a, B55, B76, B105, B106, D16 (kept, per B61) | Specs: [`../specs/agents.md`](../specs/agents.md) §6 (Cross-Agent Mechanisms), [`../specs/api.md`](../specs/api.md) (`/models`, `/settings`), [`../specs/security.md`](../specs/security.md) (Model calls)

Implementation note (historical, before the 2026-11-10 status below): Research reads the saved chat mode and forwards its resolved
model through its graph. Until account model discovery and the global blocklist are
wired, Research accepts only its configured runtime default and rejects a different
saved selection instead of silently ignoring it. The deployment configuration
passes `CEI_DEFAULT_MODEL_ID` to the Router and Research runtime. Legacy null mode
and model fields are filled conditionally so a concurrent settings update wins.
This temporary restriction is not the approved final model
selection behavior. Drafting, title-model policy, Guardrails, discovery, settings,
and prompt caching remain incomplete; no deployed behavior has been verified.
The current runtime IAM policy permits only Anthropic inference profiles and
foundation models. A configured non-Anthropic default can therefore fail with
an IAM denial even though the value is propagated. Do not broaden inference
permissions until model discovery and global blocklist enforcement are in place.

Implementation note (guardContent scope, wave 3): `shared/utils.get_model()` now
returns a `GuardScopedChatBedrockConverse` (`shared/runtime/guarded_input.py`)
whenever a Guardrail is configured; it checks the converted Bedrock Converse
request and raises `GuardContentRequiredError` if no `guardContent` block is
present, unless the call site opts out with `get_model(..., user_text=False)`
(for calls that send no user-typed text). Every `get_model()` call site under
`backend/agents` now either wraps the user's typed text (query, clarification,
scope/outline correction, revision feedback) in a `guard_content` block via
`shared.runtime.guarded_input.user_turn()`/`content_blocks()`, or declares
`user_text=False`. Retrieved evidence, tool results, recalled long-term memory,
and system prompts stay plain text, so the Guardrail's input masking (B51a)
never reaches paper authors, patent assignees, or FDA contact data. With no
Guardrail configured, `get_model()` is unchanged from before this phase.

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

## Implementation status (2026-11-10)

- Blocklist: the built-in D3 patterns (`shared/config.py` `D3_BLOCKED_MODEL_PATTERNS`) merged with `CEI_BLOCKED_MODELS` (deploy config `blockedModelIds`). The env var reaches the runtimes, the Router, SessionMetadata, and ModelSettings.
- Guardrail: `ModelGuardrail` is now in `BackendStack`. `get_model()` (`shared/utils.py`) attaches it through the Converse `guardrailConfig`, with trace disabled. Each runtime role gets `bedrock:ApplyGuardrail` on that one ARN.
- Discovery: `shared/runtime/model_discovery.py` uses `ListFoundationModels`, `ListInferenceProfiles`, and `GetInferenceProfile`.
- Routes: the new `models-settings-handler` Lambda (`constructs/model-settings.ts`) serves `GET /models` and `GET`/`PUT /settings`. The new UserMetadataTable holds `default_model_id` and `default_mode`.
- Sessions: `POST /sessions` validates `model_id` against discovery and the blocklist (fail closed). `PATCH /sessions/{id}` still accepts only `title` and `mode`.
- Frontend: a model picker for new chats only; it locks after the chat starts. The Settings page edits the defaults.
- Inference IAM is widened to all foundation models and this account's inference profiles (B105) — superseding the earlier "Anthropic-only" restriction above. A user can pick any Bedrock model, not only Anthropic models.
- `GET /models` and the write-time check (B105) show only models with: text input/output, response streaming, `ON_DEMAND` or `INFERENCE_PROFILE` inference, not `LEGACY`, and a family in the fixed provider allowlist that supports Converse tool use (Anthropic Claude, Amazon Nova, Meta Llama 3.1+, Mistral Large/Pixtral Large, Cohere Command R, AI21 Jamba, DeepSeek, Writer Palmyra X, Qwen, OpenAI gpt-oss). The D3 blocklist still applies after that filter.
- Prompt caching is built in code (`cachePoint` on the static system prompt and tool definitions, in `get_model()`), per B105. It needs live AWS verification — see the list below. `cachePoint` is sent only to models that support it (Claude 3.5 Haiku, Claude 3.7 Sonnet, Claude 4+, Amazon Nova); other models get no cache benefit, a trade-off the user accepted.
- Gap: the saved `default_model_id`/`default_mode` are stored, but no code applies them to a new chat yet.

## Implementation status (2026-11-10, wave3/settings-defaults)

- `POST /sessions` (`backend/lambdas/session-metadata-handler/metadata_handler.py` `_create`): when the request omits `model_id`, the handler now reads the caller's profile from `UserMetadataTable` (`dynamodb:GetItem`, read-only grant added in `backend-stack.ts`) and re-validates the saved `default_model_id` with the same fail-closed blocklist/discovery check as an explicit selection. A still-available saved default is used; a now-blocklisted/undiscovered saved value, a missing profile, or a table read failure all fall back to `config.default_model_id` (the global default) rather than failing the create. `mode` is unchanged — it stays a required field on `POST /sessions`, so `default_mode` is not applied there.
- Gap, with the fix now decided by the user (2026-11-10): the *implicit* session-creation path — a first turn arriving through `begin_turn`/`resolve_turn_config` (`backend/agents/shared/runtime/turn_config.py`) for a session row that has no stored `model_id`/`mode` — does not consult `UserMetadataTable` at all; it fills the missing value with `config.default_model_id`/`"science"` directly via `initialize_turn_settings`. Decided fix: an older chat with no saved model uses the user's saved default model from `UserMetadataTable`, so each of the 4 runtimes needs its own `dynamodb:GetItem` grant on that table. That module is owned by another workstream (`backend/agents/shared/runtime/**`); applying the user's saved defaults there needs its own change: look up `UserMetadataRepository(user_id).get()` (already present in `shared/persistence/user_metadata.py`) before calling `initialize_turn_settings`, with the same re-validation and fail-open-to-global-default behavior as `_create` above.

## Needs live AWS verification

- `GET /models` returns the real account model list, with blocked models removed.
- `PUT /settings` then `GET /settings` round-trip on the deployed UserMetadataTable.
- `POST /sessions` with a not-available `model_id` returns `400`.
- The Guardrail masks synthetic PII in user input, and does not mask tool results or model output.
- No Guardrail ID or model ID shows in a trace or error payload.
- A chat that uses a non-default Anthropic model runs end to end.
- A non-Anthropic model (for example a Nova model and a Llama model) runs a full turn with tools and the Guardrail.
- `cachePoint` usage shows `cacheReadInputTokens > 0` on a Claude model.
- No `ValidationException` on a non-caching model (one outside the Claude 3.5 Haiku/3.7 Sonnet/4+/Nova list).
- An older chat with no saved model uses the user's saved default model.
