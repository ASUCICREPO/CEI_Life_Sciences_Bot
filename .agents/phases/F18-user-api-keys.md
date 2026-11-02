# F18 — User API keys
Status: not started | Depends on: F4 | Decisions: B18, B20, B76, B78, B94 | Specs: ../specs/data-model.md, ../specs/api.md, ../specs/auth-and-tool-scope.md

## Goal
Let a user add their own, optional API key for a data source (for example NCBI) to get a higher rate limit, and see which sources they have configured — through a real Settings page and Secrets Manager, not a mock.

## Scope — In / Out

**In**
- `GET`/`PUT /settings` (`default_model_id`, `default_mode`, `configured_sources` — values only, per `api.md`).
- `PUT`/`DELETE /settings/api-keys/{sourceId}` — write/remove a user's key; the key value is never returned by any route, including this one's own response (B20, `api.md`).
- Secrets Manager secret naming `cei/user-keys/{user_id}/{source_id}` (B78 — this was a proposal in `data-model.md`; B78 fixes it).
- Gateway Lambda read pattern (**B94: this phase must decide how the user id reaches the Lambda** — the Gateway does not forward the token to Lambda targets; recommended: a Gateway request interceptor that passes the verified `sub` on, wired into `user_id_resolver` in `backend/lambdas/shared/data_source_runtime.py`): read the user's `sub` from the forwarded token, look up `cei/user-keys/{sub}/{source_id}`, fall back to the free/unauthenticated path if no key exists (already how all 10 sources work today) — this phase's job is the *user-facing* half (settings + secret CRUD); the *Lambda-side* read is implemented once inside each F4-built Gateway Lambda, referencing this phase's naming convention.
- Settings page port: wire the founder-ai-dashboard `SettingsPage.tsx`'s "Data source connections" list to the real settings API, replacing `src/mocks/tools.ts`'s static `configuredToolSourceIds`.

**Out**
- Per-user model restriction beyond the global blocklist — B76 explicitly rules this out ("No per-user model restriction. Only the global blocklist").
- Design of paid, user-configured sources beyond the 10 free + NCBI (deferred, see `later.md`).
- The "AI providers" and "Confidentiality" sections shown in the `SettingsPage.tsx` prototype — the AI-providers picker belongs to F8 (model picker), and the confidentiality-level/export-review toggles shown in the mock do not correspond to any B-decision; treat them as **Proposal (not decided)**, out of scope for this phase.

## Deliverables
- `backend/agents/shared/persistence/user_metadata.py` (new) — repository for the User Metadata Table (referenced only by `config.user_metadata_table` today; no repository code exists), following the same `BaseXxxRepository`/concrete-class pattern as `chat_metadata.py`.
- `backend/lambdas/settings/handler.py` (new) — implements `GET/PUT /settings` and `PUT/DELETE /settings/api-keys/{sourceId}`.
- `backend/lib/backend-stack.ts` — add the User Metadata Table (if F3 has not already created it — `data-model.md` flags the exact phase for base read access as a Proposal; this phase must create the table if it does not exist yet), the Settings Lambda, its 4 routes, and IAM scoping so the Lambda can only read/write `cei/user-keys/{caller_sub}/*`, never another user's prefix.
- `frontend/src/pages/SettingsPage.tsx` (once ported, F2) — wire the "Data source connections" section to the real API; remove `src/mocks/tools.ts`'s hardcoded `configuredToolSourceIds` in favor of a real settings fetch.

## Tasks
- [ ] 1. Confirm whether F3 already created the User Metadata Table; if not, create it here with the schema `data-model.md` §4 proposes (`PK = user_id`, `SK = "profile"`).
- [ ] 2. Implement `UserMetadataRepository` (`get`, `put`, `update`).
- [ ] 3. Implement `GET/PUT /settings`.
- [ ] 4. Implement `PUT /settings/api-keys/{sourceId}` — writes the secret, updates `configured_sources` on the User Metadata Table item, never echoes the key value back.
- [ ] 5. Implement `DELETE /settings/api-keys/{sourceId}` — removes the secret and updates `configured_sources`.
- [ ] 6. Add IAM policy scoping so the Settings Lambda's `secretsmanager:PutSecretValue`/`GetSecretValue`/`DeleteSecret` calls are restricted to `cei/user-keys/{caller_sub}/*` at call time (derive the prefix from the authenticated request, never trust a client-supplied `user_id`).
- [ ] 7. Wire `SettingsPage.tsx`'s data-source list to the real `GET /settings` response.
- [ ] 8. Wire the "Configure" button flow to `PUT /settings/api-keys/{sourceId}`.

## Acceptance criteria
- A user can add a key for one source and it appears in `configured_sources` on the next `GET /settings`; the key value itself is never present in any response body.
- Deleting a key removes it from Secrets Manager and from `configured_sources`.
- A request for another user's secret prefix is denied at the IAM layer, not just the application layer (verified by an IAM policy test, not only a Lambda-code check).
- The settings page shows exactly the sources the backend reports as configured — no hardcoded list.

## Tests
- Unit (pytest): `UserMetadataRepository` CRUD against a mocked DynamoDB table.
- Unit: Settings Lambda — add key, remove key, get settings, put settings; assert the key value is absent from every response.
- CDK assertion + cdk-nag: User Metadata Table schema; Secrets Manager IAM policy is scoped to a `cei/user-keys/${aws:userid}`-style condition or an equivalent per-call prefix check, not a blanket `cei/user-keys/*` grant.
- Integration: end-to-end add-key → Gateway Lambda picks it up (once F4's Gateway Lambda read pattern exists) → higher-rate-limit path is used instead of the free path (can be a mocked assertion on which code path ran, not a real rate-limit test).
- Frontend (vitest, once ported): Settings page renders configured/not-configured state from a fetched settings object, not from `src/mocks/tools.ts`.

## Risks and open items
- **Proposal (not decided), carried from `data-model.md`:** whether `User Metadata Table`'s base read/write access belongs to F3 or is bundled into this phase — this file assumes F18 creates the table if F3 has not, to avoid blocking on an ordering ambiguity; flag for the roadmap owner to resolve cleanly.
- **Proposal (not decided), carried from `data-model.md`:** whether the User Metadata Table's `email` field is duplicated from Cognito or read live — not resolved here; pick "duplicated, refreshed on settings write" as the simplest option if this phase needs a value, but do not treat it as decided.
- B76 (no per-user model restriction) means this phase must not add a `blocked_model_overrides`-style field even though `data-model.md` lists it as a possible attribute — explicitly excluded by B76.

## Handoff notes
- F4's Gateway Lambdas must read the exact secret name convention this phase fixes (`cei/user-keys/{user_id}/{source_id}`) — if F4 ships before this phase, its Lambda-side lookup code should already assume this naming, since it is fixed by B78, not invented here.
- F19 (data deletion) removes every secret under `cei/user-keys/{user_id}/*` on account delete — this phase's naming convention is exactly what F19's cascade targets.
