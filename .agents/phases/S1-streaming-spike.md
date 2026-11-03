# S1 — Streaming spike

Status: not started | Depends on: — | Decisions: B2, B3, B4, B23/B79 (10-minute async threshold, decided), B68 | Specs: [`../specs/architecture.md`](../specs/architecture.md) §2.1, [`../specs/streaming-protocol.md`](../specs/streaming-protocol.md), [`../specs/api.md`](../specs/api.md) (`/chat/stream`), [`../specs/auth-and-tool-scope.md`](../specs/auth-and-tool-scope.md) §9

## Goal

Prove that one Regional REST API Gateway route (`responseTransferMode: STREAM`), with a Cognito User Pools authorizer, sitting behind CloudFront, can carry SSE from a Node.js Lambda for at least 10 minutes without CloudFront or API Gateway silently closing the connection. This spike answers the question before F5 builds the real Session Runner and Research agent on top of it.

## Scope — In / Out

**In:** a throwaway CDK stack (or a stack addition, spike-only, not merged into `BackendStack` production resources) with one REST API, one STREAM route, one minimal Node.js Lambda that emits SSE heartbeats and dummy `text_delta`-shaped events, the Cognito authorizer attached to that route, and a CloudFront distribution in front of the API. A minimal `fetch` + `ReadableStream` test client (can be a Node.js script or a browser page) that authenticates with a real Cognito token and reads the stream to completion.

**Out:** the real Session Runner Lambda, the real Session Router, any AgentCore Runtime call, any production CDK resources, the reconnect/resume mechanism (B68) beyond confirming the sequence-numbered event write path is feasible.

## Deliverables

- `backend/lib/spikes/streaming-spike-stack.ts` (new, spike-only CDK construct; not referenced by `bin/backend.ts`'s production stack)
- `backend/lambdas/spike-streaming-runner/` (new, Node.js, minimal SSE emitter using the same event framing as `backend/agents/shared/transport/sse.py` — `id:`/`event:`/`data:` frames, `: heartbeat` comments every 15–20s per B4)
- A findings note at `.agents/phases/findings/S1.md`

## Tasks

- [ ] 1. Add a spike CDK stack with one Regional REST API, one resource/method with `responseTransferMode: STREAM`, and a Cognito User Pools authorizer (reuse or stub a minimal user pool if F2's real pool does not exist yet).
- [ ] 2. Write the Node.js Lambda: on invoke, stream `surface_begin`, a handful of `component_add`/`text_delta` events, `: heartbeat` every 15–20s, and `surface_end`, using sequence-numbered `id:` frames matching the framing already implemented in `shared/transport/sse.py`.
- [ ] 3. Deploy the spike stack to a real AWS account/Region and get a real Cognito ID token for a test user.
- [ ] 4. Run the test client against the deployed endpoint through CloudFront; confirm the authorizer accepts the Cognito token on the STREAM route (this exact combination is the least-documented part per the decision log's research facts).
- [ ] 5. Force a run past 10 minutes (pad with extra heartbeats) and confirm neither CloudFront nor API Gateway drops the connection before the 15-minute cap; confirm the 5-minute idle timeout does not fire as long as heartbeats keep flowing.
- [ ] 6. Confirm CloudFront is configured `CachingDisabled` with no compression on the spike route, and that this is required (not optional) for the stream to render progressively rather than buffer.
- [ ] 7. Write the findings note: what worked, what didn't, any CloudFront/API Gateway settings that were not obvious from AWS docs, and a recommendation on whether B2/B4 need any amendment.
- [ ] 8. Tear down the spike stack (or leave it isolated under a clearly spike-only stack name so it is not accidentally included in a production deploy).

## Acceptance criteria

- A Cognito-authenticated `fetch` + `ReadableStream` client receives all emitted SSE events, in order, over the STREAM route through CloudFront.
- A run lasting at least 10 minutes (padded with heartbeats) completes without a dropped connection.
- The Cognito authorizer rejects a request with no token or an expired token on the same STREAM route.
- The findings note answers every question in "Spike questions to answer" below with a pass/fail verdict.

## Tests

- No unit tests required (this is an infrastructure spike). Manual/scripted verification only, recorded in the findings note.
- If time allows, a small integration script (`backend/lambdas/spike-streaming-runner/test-client.mjs` or similar) that asserts event ordering and heartbeat cadence programmatically.

## Spike questions to answer

Findings note: `.agents/phases/findings/S1.md`.

1. Does the Cognito User Pools authorizer work unmodified on a STREAM-mode method, or does it need a different integration type? Pass: authorizer accepts/rejects tokens correctly on the STREAM route. Fail: authorizer errors, is silently skipped, or requires a REST API feature not available on STREAM routes — this would force a re-look at B2.
2. Does the connection survive past the 5-minute idle timeout when only heartbeats (no real data) are flowing? Pass: confirms the heartbeat interval in B4 (15–20s) is sufficient. Fail: changes the required heartbeat interval.
3. Does CloudFront need explicit `CachingDisabled` + no compression to avoid buffering the response before it reaches the client? Pass/fail confirms or changes the CloudFront config assumption in B4.
4. Is the 15-minute maximum and 2 MB/s-after-10MB throttle observed as documented? Pass: matches research facts already in the decision log (informational only, no B4 change expected). Fail: changes the numbers used in `streaming-protocol.md`'s "Validation limits" table.

## Risks and open items

- B79 fixes the async threshold at 10 minutes (non-pipeline runs go async above that estimate, leaving a 5-minute margin under the 15-minute stream cap, and there is no mid-run handoff); this spike does not need the value (spike runs are always short-lived by design) but F5 will use it once the real Research agent is live.
- B68 (stream reconnect via Chat History Store sequence numbers) is not tested here — this spike only confirms the underlying SSE transport works; reconnect logic is F6 scope.
- If the Cognito-on-STREAM combination fails outright, this blocks F5's basic assumption and needs a decision-log discussion with the user before F3/F5 proceed.

## Handoff notes

- F5 (Research agent live) and F3 (API + session store) both assume the STREAM route + Cognito authorizer combination works as designed in `architecture.md` and `auth-and-tool-scope.md` §9. If this spike fails any acceptance criterion, F3/F5 must not start until the user has been consulted and, if needed, a new decision is recorded.
- The heartbeat/event-framing code written here should be treated as a throwaway proof, not reused verbatim in production — F5's real Session Runner is Node.js per B3, but its logic (Router call, error handling, redaction) is new work in F5, not a copy of this spike's Lambda.
