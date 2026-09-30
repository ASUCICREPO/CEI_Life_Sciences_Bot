# F02 — Hosting + auth

Status: not started | Depends on: F1 | Decisions: B1, B12, B13, B14, B16, B53, B66 | Specs: [`../specs/architecture.md`](../specs/architecture.md) §1 (S3/CloudFront/WAF/Cognito rows), [`../specs/auth-and-tool-scope.md`](../specs/auth-and-tool-scope.md) §1–2, [`../specs/security.md`](../specs/security.md) (network/edge, identity and access)

## Goal

Serve the `founder-ai-dashboard` SPA (copied into `frontend/`, replacing the archived Next.js app per B66) from S3 + CloudFront + WAF, with real Cognito self sign-up, email verification, and a domain allowlist. A user can sign up, verify, log in, and see the dashboard shell with no working chat yet.

## Scope — In / Out

**In:** copying `founder-ai-dashboard` into `frontend/`, removing its report-page mocks (B53: Target Assessment, Variant Report, Regulatory Brief), archiving the current Next.js `frontend/` to `archive/frontend-next/`, Cognito user pool + SPA app client (PKCE) + pre-sign-up domain-allowlist trigger, S3 static bucket + CloudFront (with SPA fallback routing) + WAF, and wiring the SPA's login flow to real Cognito.

**Out:** the Router M2M client and resource server/scopes (F4), the REST API and any chat functionality (F3/F5), removing the SPA's mock data files entirely (only the three report pages are removed per B53; other mocks can stay as placeholders until their real phase lands, but must not be reachable from primary navigation if they depend on removed pages).

## Deliverables

- `frontend/` (replace contents) — copy of `C:\Users\aadit\Projects\CIC\new-repo\founder-ai-dashboard\src` and its Vite/Tailwind config, per B1/B66. Remove `src/pages/TargetAssessmentPage.tsx`, `src/pages/VariantReportPage.tsx`, `src/pages/RegulatoryBriefPage.tsx` and their routes in `src/App.tsx` (per B53).
- `archive/frontend-next/` (new) — move of the current `frontend/` (Next.js app, CEI UI 1.1 renderer, zod schema, local proxy routes) for reference, per B66.
- `frontend/src/lib/auth/` (new) — Cognito PKCE login/logout/token-refresh logic (e.g. using `amazon-cognito-identity-js` or `oidc-client-ts`; pick one and record the choice here once implemented).
- `backend/lib/backend-stack.ts` (modify) — add: Cognito `UserPool` (self sign-up, email verification, no MFA per B14), a pre-sign-up Lambda trigger construct (domain allowlist, reading the allowlist from F1's config loader), an SPA `UserPoolClient` (public, PKCE, no secret), an S3 bucket + `CloudFront` distribution (OAC, SPA 403/404→`index.html` fallback) serving the built `frontend/` assets, and a `WebACL` (WAF) with AWS-managed rule groups + a rate-based rule attached to the CloudFront distribution.
- `backend/lambdas/pre-signup-domain-check/` (new, Python) — the pre-sign-up trigger Lambda.
- `frontend/vite.config.ts` (modify, if needed) — confirm the production build output matches the S3/CloudFront origin path expectations.

## Tasks

- [ ] 1. Copy `founder-ai-dashboard`'s `src/`, `package.json`, `vite.config.ts`, and related config files into `frontend/`, replacing the existing Next.js project's application code (keep `frontend/` as the directory name per the monorepo convention in `CLAUDE.md`).
- [ ] 2. Move the current `frontend/` (Next.js) to `archive/frontend-next/` before overwriting, per B66 — verify with `git status` that the move preserves history reasonably (a straightforward move + commit, not a delete-then-recreate).
- [ ] 3. Remove `TargetAssessmentPage.tsx`, `VariantReportPage.tsx`, `RegulatoryBriefPage.tsx` and their `react-router` routes/nav entries (`src/App.tsx`, `src/config/platformNavigation.ts`, `src/components/shell/LeftRail.tsx` — check all three for references) per B53.
- [ ] 4. Add the Cognito `UserPool` construct in `backend-stack.ts`: self sign-up enabled, email as the sign-up attribute, email verification required, no MFA.
- [ ] 5. Write and attach the pre-sign-up Lambda trigger (`backend/lambdas/pre-signup-domain-check/`): reads the allowed-domains list from F1's config, rejects sign-up if the email domain does not match.
- [ ] 6. Add the SPA `UserPoolClient`: public client, Authorization Code + PKCE flow, no client secret, callback/logout URLs pointing at the CloudFront domain.
- [ ] 7. Build `frontend/src/lib/auth/`: PKCE login redirect, callback handling, token storage (memory + refresh, not `localStorage` for access tokens if avoidable), logout.
- [ ] 8. Add the S3 static bucket (private, OAC) + CloudFront distribution construct with an SPA fallback (custom error response: 403/404 → `/index.html`, HTTP 200) and the WAF `WebACL` (AWS-managed rule groups + a rate-based rule per `security.md`'s T5 mitigation) associated with the distribution.
- [ ] 9. Confirm CORS/CSP posture per `security.md`'s open items: the API origin restriction is F3 scope, but the CloudFront response headers policy (CSP top-level header) should be stubbed here even though its Artifact-sandbox interaction (per `docs/cei-ui-protocol.md` "CSP inheritance") is not fully solved until later — record whatever is deferred explicitly as a TBD, do not silently skip it.
- [ ] 10. Manually verify: sign up with an allowed-domain email, receive verification, verify, log in, land on the dashboard shell with no report pages in navigation.

## Acceptance criteria

- `npm run build` in `frontend/` produces a static SPA build deployable to S3.
- A sign-up attempt with a disallowed email domain is rejected by Cognito (via the pre-sign-up trigger) before an account is created.
- A sign-up attempt with an allowed domain succeeds, requires email verification, and results in a working login via PKCE against the SPA client.
- CloudFront serves the SPA correctly on a deep link (a non-root path loads `index.html` via the fallback rule, not a CloudFront/S3 403/404 page).
- WAF is attached to the CloudFront distribution and blocks a basic scripted abuse pattern (manual smoke test, e.g. a rapid-fire request burst against the rate-based rule).
- The three report-mock pages are unreachable from the SPA's navigation and routes.
- `archive/frontend-next/` contains the prior Next.js app, unmodified except for its new location.
- `npx cdk synth` for `backend-stack.ts` passes cdk-nag with all suppressions in ADR format.

## Tests

- Frontend: `npm run lint` (oxlint, per `founder-ai-dashboard`'s existing script) passes on the copied+modified codebase.
- CDK assertion tests (`backend/test/`) for: `UserPool` has email verification and no MFA configured, the pre-sign-up trigger is attached, the SPA client has no secret and uses PKCE, the S3 bucket is private with OAC, the CloudFront distribution has the SPA fallback custom error responses, and the WAF `WebACL` is associated with the distribution.
- Unit test for the pre-sign-up Lambda (`backend/lambdas/pre-signup-domain-check/`): allowed domain passes, disallowed domain rejects, malformed email rejects safely.
- Manual/e2e (documented, not necessarily automated in this phase): full sign-up → verify → login → dashboard-shell-loads flow against a real deployed environment.

## Risks and open items

- The exact Cognito client library for the SPA (`amazon-cognito-identity-js` vs. `oidc-client-ts` vs. a hand-rolled PKCE flow) is not decided by the log — pick one during implementation and record the choice in this file's Handoff notes so F3+ frontend work is consistent.
- CORS allowed-origins configuration for non-production/local development is an open TBD in `security.md` — do not add a wildcard or a permanent "allow localhost" rule without flagging it; if local dev needs it, gate it behind a `deploy.sh`-configured flag, and note the decision is a proposal, not settled.
- CSP header content is explicitly open (`security.md` TBD, and `docs/cei-ui-protocol.md`'s "CSP inheritance" residual risk) — this phase should not attempt to fully solve it; ship a reasonable baseline CSP and record what remains open for F6/F21.

## Handoff notes

- F3 (API + session store) adds the REST API and needs the Cognito User Pool's ID/ARN (for the authorizer) and the SPA client's ID — both should be exposed as CDK outputs from this phase's stack additions.
- F4 (Data tools) needs the Cognito **resource server** and the Router **M2M client** — those are explicitly out of scope here; F4 adds them to the same `UserPool` this phase creates.
- F6 (Streaming UI) and later frontend phases build inside the `frontend/` tree this phase establishes; they should follow the founder-ai-dashboard's existing design-system conventions (`frontend/src/design-system/`) rather than reintroducing the old Next.js component patterns from `archive/frontend-next/`.
