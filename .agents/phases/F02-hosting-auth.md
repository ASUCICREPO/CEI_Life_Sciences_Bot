# F02 — Hosting + auth

Status: done (manual deployed verification passed, verified by the user on 2026-10-05) | Depends on: F1 | Decisions: B1, B12, B13, B14, B16, B53, B66, B88, B89, B90 | Specs: [`../specs/architecture.md`](../specs/architecture.md) §1 (S3/CloudFront/WAF/Cognito rows), [`../specs/auth-and-tool-scope.md`](../specs/auth-and-tool-scope.md) §1–2, [`../specs/security.md`](../specs/security.md) (network/edge, identity and access)

**B112 (2026-11-19, built in code, not yet deployed) adds to this phase's scope:** a one-time post-login onboarding page (`frontend/src/pages/`, a new `/onboarding` route, a `RequireOnboarding` gate in `App.tsx`, `profileClient.ts`) collecting First name, Last name, and Date of birth, and the SPA client's `readAttributes` addition (now includes `given_name`, `family_name`, `birthdate`), so the ID token carries them. `writeAttributes` stays empty. Needs live AWS verification. See `.agents/decision-log.md` B112 and `.agents/specs/auth-and-tool-scope.md` §14.

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

- [x] 1. Copy `founder-ai-dashboard`'s `src/`, `package.json`, `vite.config.ts`, and related config files into `frontend/`, replacing the existing Next.js project's application code (keep `frontend/` as the directory name per the monorepo convention in `CLAUDE.md`).
- [x] 2. Move the current `frontend/` (Next.js) to `archive/frontend-next/` before overwriting, per B66 — verify with `git status` that the move preserves history reasonably (a straightforward move + commit, not a delete-then-recreate).
- [x] 3. Remove `TargetAssessmentPage.tsx`, `VariantReportPage.tsx`, `RegulatoryBriefPage.tsx` and their `react-router` routes/nav entries (`src/App.tsx`, `src/config/platformNavigation.ts`, `src/components/shell/LeftRail.tsx` — check all three for references) per B53.
- [x] 4. Add the Cognito `UserPool` construct in `backend-stack.ts`: self sign-up enabled, email as the sign-up attribute, email verification required, no MFA.
- [x] 5. Write and attach the pre-sign-up Lambda trigger (`backend/lambdas/pre-signup-domain-check/`): reads the allowed-domains list from F1's config, rejects sign-up if the email domain does not match.
- [x] 6. Add the SPA `UserPoolClient`: public client, Authorization Code + PKCE flow, no client secret, callback/logout URLs pointing at the CloudFront domain.
- [x] 7. Build `frontend/src/lib/auth/`: PKCE login redirect, callback handling, token storage (memory + refresh, not `localStorage` for access tokens if avoidable), logout.
- [x] 8. Add the S3 static bucket (private, OAC) + CloudFront distribution construct with an SPA fallback (custom error response: 403/404 → `/index.html`, HTTP 200) and the WAF `WebACL` (AWS-managed rule groups + a rate-based rule per `security.md`'s T5 mitigation) associated with the distribution.
- [x] 9. Confirm CORS/CSP posture per `security.md`'s open items: the API origin restriction is F3 scope, but the CloudFront response headers policy (CSP top-level header) should be stubbed here even though its Artifact-sandbox interaction (per `docs/cei-ui-protocol.md` "CSP inheritance") is not fully solved until later — record whatever is deferred explicitly as a TBD, do not silently skip it.
- [x] 10. Manually verify: sign up with an allowed-domain email, receive verification, verify, log in, land on the dashboard shell with no report pages in navigation.

## Implementation notes

- **WAF stack split (B88).** `backend/lib/edge-waf-stack.ts` defines `EdgeWafStack`, a small stack that only holds the `CLOUDFRONT`-scope `CfnWebACL`. It always deploys to `us-east-1`. `backend/bin/backend.ts` creates it (when a deploy config exists) and passes its `webAclArn` output into `BackendStack` via `crossRegionReferences: true`.
- **Build location (B91).** The `us-east-1` `CDKToolkit` bootstrap that `EdgeWafStack` needs now runs inside CodeBuild (`buildspec.yml`), not locally in `deploy.sh`. `deploy.sh` only checks, read-only, whether `us-east-1` already has `CDKToolkit`, to decide if the CodeBuild role needs the one-time bootstrap policy.
- **Login library (B89).** `oidc-client-ts` 3.5.0. Cognito managed login (hosted pages), Authorization Code + PKCE. Tokens live in memory only (`InMemoryWebStorage`); OAuth state lives in `sessionStorage`. Cognito's OIDC discovery document has no `end_session_endpoint`, so `frontend/src/lib/auth/userManager.ts` builds the logout URL by hand: `<cognitoDomain>/logout?client_id=…&logout_uri=<origin>/`.
- **Password policy.** Minimum 12 characters, upper + lower + digit + symbol required. `AccountRecovery.EMAIL_ONLY`. MFA off (B14).
- **Token lifetimes.** Access and ID tokens: 60 minutes. Refresh token: 7 days.
- **Cognito domain.** Prefix `cei-ls-<account-id>`, managed login version 2 (`CfnManagedLoginBranding` with `useCognitoProvidedValues: true`).
- **WAF rules (`EdgeWafStack`).** `AWSManagedRulesCommonRuleSet`, `AWSManagedRulesKnownBadInputsRuleSet`, `AWSManagedRulesAmazonIpReputationList`, and a rate-based rule (1000 requests / 5 minutes per IP — a proposal; `security.md`'s T5 mitigation values stay TBD).
- **CSP baseline.** The CloudFront `ResponseHeadersPolicy` on `BackendStack`'s `Distribution` sets `default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self' https://cognito-idp.<region>.amazonaws.com https://<prefix>.auth.<region>.amazoncognito.com; frame-ancestors 'none'; object-src 'none'; base-uri 'self'; form-action 'self' https://<prefix>.auth.<region>.amazoncognito.com`, plus HSTS, `nosniff`, frame `DENY`, and a strict referrer policy. How this interacts with the Artifact iframe stays a TBD for F6/F21.
- **Local dev flag (B90).** `deploy.sh --allow-localhost-callback` (also a `y/N` prompt) sets `allowLocalhostCallback: true` in `cei.deploy.json`. When true, `BackendStack` adds `http://localhost:5173/auth/callback` and `http://localhost:5173/` as extra Cognito callback/logout URLs. Default is `false`. This is a proposal for the `security.md` CORS/dev-origin TBD, not a settled decision.
- **cdk-nag suppressions (ADR format, on `BackendStack` unless noted).** `AwsSolutions-COG2` (no MFA, B14). `AwsSolutions-COG8` (the user pool stays on the Essentials feature plan, not Plus — Plus-tier threat protection has a cost not yet approved). `AwsSolutions-CFR1` (no CloudFront geo restriction — no approved country list yet). `AwsSolutions-CFR4` (default `*.cloudfront.net` certificate — no custom domain/ACM certificate provisioned yet). `AwsSolutions-CFR2` (only on the environment-agnostic `cdk synth` path, where no deploy config exists yet and so `EdgeWafStack`/`webAclArn` do not exist either; every real deploy attaches the web ACL). `AwsSolutions-L1`/`IAM4`/`IAM5` on the CDK-owned `BucketDeployment` custom-resource handler (a singleton `aws-cdk-lib` provisions itself, not this stack's code).
- **BucketDeployments.** Two, in order: `SiteAssetsDeployment` (the built SPA from `frontendAssetPath`) runs first, then `RuntimeConfigDeployment` writes `/config.json` (`Source.jsonData`, `Cache-Control: no-cache`, `prune: false` so it does not delete the SPA assets the first deployment just wrote).
- **Stack outputs.** `BackendStack`: `UserPoolId`, `UserPoolArn`, `UserPoolClientId`, `CognitoDomain`, `DistributionDomainName`, `SiteUrl`. `EdgeWafStack`: the web ACL ARN (`WebAclArn` output).
- **Security review fixes (2026-09-30).** Email is `mutable: false`. The SPA client allows only the hosted UI + refresh (`ExplicitAuthFlows: ['ALLOW_REFRESH_TOKEN_AUTH']`, set with an escape hatch because CDK omits the property otherwise, and Cognito then enables SRP/custom auth by default). `WriteAttributes: []`, so a user cannot change the email after the domain check. `backend/lib/app.ts` `buildStacks()` holds the two-stack wiring, and `backend/test/app.test.ts` runs cdk-nag on the cross-Region reference resources. The pre-sign-up Lambda requires exactly one `@` and an ASCII `[a-z0-9.-]` domain. The CFR2 suppression applies only when no web ACL exists. The logs bucket expires objects after 90 days. The response headers add `Permissions-Policy` and `Cross-Origin-Opener-Policy: same-origin`. Sign-out revokes the refresh token (`<cognitoDomain>/oauth2/revoke`), and the callback accepts only a same-app `returnTo` path. `deploy.sh` hides the PAT input on a TTY.
- **Security review items deferred:** `style-src 'unsafe-inline'` and the API origin in `connect-src` (F3, F6/F21); a REGIONAL WAF on the Cognito user pool against sign-up spam and credential stuffing (F3/F4); the admin email in a plaintext CodeBuild environment variable (F21); scoped `--cloudformation-execution-policies` for CDK bootstrap in both Regions (F21).
- **Deferred TBDs carried forward:** the CSP/Artifact-iframe interaction (F6/F21); WAF logging (F20/F21); Cognito's default email sender (50 emails/day limit) — SES is a later item; the WAF rate value (1000 req/5 min per IP) and the localhost-callback flag are proposals, not settled security decisions; the NCBI Vite dev proxy is still in place (B21 removes it in F15). The real `./deploy.sh` run and the manual sign-up/login/WAF smoke tests (task 10 above) have not been done yet.

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

- **Resolved (B89):** the Cognito client library for the SPA is `oidc-client-ts` 3.5.0, with Cognito managed login and Authorization Code + PKCE. See Implementation notes above.
- **Resolved (B90), still a proposal:** local dev's `allowLocalhostCallback` config flag (default `false`, set by `deploy.sh --allow-localhost-callback`) gates the extra `localhost:5173` Cognito callback/logout URLs. This is a proposal for the `security.md` CORS/dev-origin TBD, not a settled decision.
- CSP header content is explicitly open (`security.md` TBD, and `docs/cei-ui-protocol.md`'s "CSP inheritance" residual risk) — this phase does not attempt to fully solve it; a reasonable baseline CSP ships now (see Implementation notes), and the Artifact-sandbox interaction stays open for F6/F21.
- WAF logging (access-log delivery for the web ACL itself, distinct from the CloudFront distribution's own logs) is not wired up yet — deferred to F20/F21.
- Cognito uses its default email sender (50 emails/day limit). SES is a later item, recorded as a risk (see the decision log's "Still open" list).

## Handoff notes

- F3 (API + session store) adds the REST API and needs the Cognito User Pool's ID/ARN (for the authorizer) and the SPA client's ID. `BackendStack` already exposes these as CDK outputs: `UserPoolId`, `UserPoolArn`, `UserPoolClientId`, `CognitoDomain`, `DistributionDomainName`, `SiteUrl`. F3's frontend work should call the SPA's `useAuth().getAccessToken()` (`frontend/src/lib/auth/AuthProvider.tsx`) to attach the bearer token to API calls.
- F4 (Data tools) needs the Cognito **resource server** and the Router **M2M client** — those are explicitly out of scope here; F4 adds them to the same `UserPool` this phase creates.
- The NCBI Vite dev proxy is still in place in `frontend/`; F15 removes it (B21).
- Deferred TBDs for later phases: the CSP/Artifact-iframe interaction (F6/F21), WAF logging (F20/F21), and Cognito's default email sender limit / SES migration (later item).
- F6 (Streaming UI) and later frontend phases build inside the `frontend/` tree this phase establishes; they should follow the founder-ai-dashboard's existing design-system conventions (`frontend/src/design-system/`) rather than reintroducing the old Next.js component patterns from `archive/frontend-next/`.
