# S2 — Tool scope spike

Status: in progress (deployed run remains) | Depends on: — | Decisions: B29, B30, B69, B70 | Specs: [`../specs/auth-and-tool-scope.md`](../specs/auth-and-tool-scope.md) (full), [`../specs/tools.md`](../specs/tools.md) §1, [`../specs/security.md`](../specs/security.md) (T3)

## Goal

Prove that Policy in AgentCore (Cedar, GA 2026-03) can enforce per-source, per-caller tool scoping on an AgentCore Gateway, including filtering what `tools/list` returns, using Cognito M2M client-credentials tokens carrying custom scopes. This spike answers B30's four explicit prototype items before F4 builds the real Gateway and the 10 source Lambdas behind it.

## Scope — In / Out

**In:** a minimal Cognito resource server with 2–3 test scopes (not the full 11-scope list), one AgentCore Gateway with 2–3 dummy Lambda targets (echo Lambdas are fine — this spike does not need real PubMed/ClinicalTrials.gov logic), a Cedar policy set starting `LOG_ONLY` then flipped to `ENFORCE` per B30, a Cognito M2M app client, and a script that requests scoped tokens and calls the Gateway.

**Out:** the real 10 source Lambdas (F4), the real 11-scope list (F4 builds the full set once this spike confirms the mechanism), the pre-token-generation trigger's production wiring into the real Session Router (that Lambda does not exist until F5/F9 — this spike can call Cognito directly from a test script instead of through the Router).

## Deliverables

- `backend/lib/spikes/tool-scope-spike-stack.ts` (new, spike-only CDK: Cognito resource server + 2–3 scopes, M2M app client, AgentCore Gateway with 2–3 echo Lambda targets, Cedar policy set)
- `backend/lambdas/spike-echo-target-a/`, `backend/lambdas/spike-echo-target-b/` (new, minimal Lambdas that just echo their input, used as Gateway targets)
- A test script (e.g. `backend/lambdas/spike-echo-target-a/test-scope.mjs` or a Python script under a spike test folder) that requests a token scoped to only target A and confirms it can call A, cannot call B, and `tools/list` shows only A's tools
- A findings note at `.agents/phases/findings/S2.md`

## Tasks

- [x] 1. Create the Cognito resource server (e.g. `cei-tools-spike`) with two scopes, `cei-tools-spike/target-a` and `cei-tools-spike/target-b`, and confirm neither scope name is a substring of the other (B69/the `like`-matching collision rule in `auth-and-tool-scope.md` §3). *(Code done: also `shared` and an unreferenced `probe` scope; Jest checks the substring rule.)*
- [x] 2. Create the M2M app client and confirm only a designated test-runner IAM identity can read its client secret from Secrets Manager (proves the IAM-boundary pattern B30 asks F4 to enforce for the real Router). *(Code done; the AccessDenied check runs in `s2_scope_test.py` after deploy.)*
- [ ] 3. Deploy the AgentCore Gateway with the two echo targets.
- [x] 4. Write the Cedar policy set: default deny, permit `initialize`/`tools/list` unconditionally, permit each target's action(s) only when the token's scope claim contains that target's scope — write it at **one permit per target** (all of a target's tools share the target-level scope), per B69. *(Code done; the Cedar shape differs from the spec examples, see findings/S2.md.)*
- [ ] 5. Start the policy set in `LOG_ONLY`. Confirm denies are logged, not blocked, and that a token with no scopes can still call both targets while `LOG_ONLY`.
- [ ] 6. Flip to `ENFORCE`. Confirm a token scoped to only `target-a` can call target A, is denied calling target B, and that `tools/list` for that token returns only target A's tools.
- [ ] 7. Confirm the shared-scope pattern from B70 (one scope shared by every caller by default) by adding a third always-granted scope and target, and confirming a caller with only `target-a` still reaches it without an explicit grant.
- [ ] 8. Deliberately trigger a denied call from inside a trivial LangGraph node (or a plain Python script simulating one) and record the exact error shape the Gateway/Policy engine returns, to inform the "denied-call error shape inside LangGraph" open item.
- [ ] 9. Test token TTL: request a 1-hour token (per B67), let it approach expiry mid-"run" (a sleep loop is fine for the spike), and confirm the internal token-refresh pattern described in B67 is technically reachable (an IAM-authenticated internal endpoint call that returns a fresh token with the same scopes) — a stub refresh endpoint is acceptable for this spike; F4 builds the real one.
- [ ] 10. Write the findings note covering all four B30 prototype items plus the B67/B69/B70 checks above.

## Acceptance criteria

- A token scoped to `target-a` only succeeds calling target A and is denied calling target B under `ENFORCE`.
- `tools/list` for that token lists only target A's tool(s), never target B's.
- The shared/default scope (B70 pattern) is reachable by a caller that never explicitly requested it.
- The M2M client secret is not readable by any IAM identity other than the designated test-runner role (simulating the real Router's exclusive access, B30 §2).
- The findings note has a pass/fail verdict for every item in "Spike questions to answer" below.

## Tests

- No unit tests in the traditional sense (this is an IAM/Policy infrastructure spike). The test script in Deliverables is the verification artifact; keep its assertions explicit (assert 200/403 as appropriate) rather than manual-only, so the findings note can cite exact results.
- CDK assertion test for the spike stack is optional (spike-only code, not merged into the production stack) but recommended if the spike stack lives more than a few days.

## Spike questions to answer

Findings note: `.agents/phases/findings/S2.md`.

1. Does Cedar's `like` operator behave as a substring match as documented in the research facts? Pass: confirms the naming-collision rule in `auth-and-tool-scope.md` §3 is real and must be followed for the full 11-scope list in F4. Fail: changes how F4 must write its permit statements.
2. Does `tools/list` filtering by scope actually work against the GA Policy engine, or does it require a workaround (e.g., client-side filtering in the Gateway Lambda instead of Policy-side)? Pass/fail directly resolves B30's "not yet verified" prototype item and determines whether F4 can rely on Policy-side filtering alone.
3. Can the runtime execution role be kept unable to read the M2M secret while the Router-equivalent role can? Pass: confirms the IAM boundary pattern for F4. Fail: means F4 needs a different secret-isolation approach (e.g., a separate account or a resource policy condition).
4. What does a denied Gateway call look like from inside LangGraph (exception type, message shape, HTTP-equivalent status via MCP)? This answers the open item in `auth-and-tool-scope.md` §8/§13 and determines what F4/F5's agent error-handling code must catch and how it should behave (retry, surface to user, or fail the turn) — not decided yet, this spike's finding informs but does not itself decide that.
5. Is the B67 token-refresh pattern (internal IAM-authed endpoint, active-run check, same-scope reissue) technically workable end to end, even with a stub endpoint? Pass: F4 can build the real refresh endpoint with confidence. Fail: B67 needs revisiting with the user.

## Risks and open items

- Tool-level vs. target-level Cedar permit granularity was an open item in `auth-and-tool-scope.md` §6/§13; B69 has since decided target-level (one permit per source target, all of that target's tools). This spike must implement and verify the B69 shape, not the older tool-level proposal.
- The shared-scope model for LS Ops/Code Interpreter (`auth-and-tool-scope.md` §3's open item) is resolved by B70 (one shared scope, granted to every principal by default) — this spike's task 7 verifies that shape works mechanically; F4/F12 build the real LS Ops target.
- Denied-call behavior inside LangGraph remains open after this spike (task/question 4 above only characterizes the error shape; deciding the agent's *response* to a deny is F4/F5 design work, possibly needing a new decision-log entry with user approval).

## Handoff notes

- F4 (Data tools) builds the real Gateway, the real 10 source Lambdas, the full 11-scope resource server, and the production Cedar policy set directly on this spike's confirmed mechanism. F4 should reuse the same `LOG_ONLY` → `ENFORCE` rollout sequence proven here.
- F5 and F15 (Research agent, Tool agents) depend on F4's Gateway being live; they do not depend on this spike directly, but any denied-call handling decision this spike surfaces as needing user input should be resolved before F5's agent code is written, to avoid rework.
