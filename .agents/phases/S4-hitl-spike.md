# S4 — HITL spike

Status: not started | Depends on: — | Decisions: B56, B57, B28 | Specs: [`../specs/agents.md`](../specs/agents.md) (HITL points), [`../specs/api.md`](../specs/api.md) (`/ui-actions`), [`../specs/streaming-protocol.md`](../specs/streaming-protocol.md) (`hitl_request`), [`../specs/data-model.md`](../specs/data-model.md) §9 (AgentCore Memory)

## Goal

Prove that a LangGraph `interrupt` inside an AgentCore Runtime graph can checkpoint via `AgentCoreMemorySaver`, end the run so the stream can surface a question, and be resumed correctly in a brand-new run once a typed action arrives — with no time limit on the wait, per B56/B57. This spike de-risks the mechanism before F13 wires real HITL points into all four agents.

## Scope — In / Out

**In:** a minimal LangGraph app with one node that calls a HITL inline function (reuse `ask_user_question` from `backend/agents/shared/tools/hitl/inline_functions.py` as-is), triggers `interrupt`, and a second run that resumes it after a simulated typed action. Manual or scripted simulation of "the user answers after a long wait" (minutes for the spike; B57 requires no limit in production).

**Out:** the real `/ui-actions` REST route and its Lambda (F13 builds this), the real production action-endpoint validation logic, wiring HITL into the actual Research/Drafting/Analysis/Pipeline Manager graphs (F13 does this once this spike confirms the mechanism works).

## Deliverables

- `backend/agents/spikes/hitl_spike/` (new, Python — `graph.py` with one `interrupt`-calling node, `main.py` entrypoint, using `AgentCoreMemorySaver`)
- A small test harness script that (a) starts a run, (b) observes the interrupt and captures the checkpoint reference, (c) waits (or simulates a wait), (d) submits a resume call with a chosen answer, (e) confirms the graph continues past the interrupt with that answer available in state
- A findings note at `.agents/phases/findings/S4.md`

## Tasks

- [ ] 1. Wire the spike graph's one node to call `ask_user_question` (existing generic tool, `shared/tools/hitl/inline_functions.py`) and trigger a LangGraph `interrupt` with the question surface as the interrupt payload.
- [ ] 2. Confirm `AgentCoreMemorySaver` writes a checkpoint at the interrupt point (inspect the AgentCore Memory store directly, or via a debug read helper) before the run ends.
- [ ] 3. Confirm the run's entrypoint returns/ends the stream at the interrupt (no `surface_end`), matching the `hitl_request`-terminates-the-stream rule in `streaming-protocol.md`.
- [ ] 4. Simulate a wait of at least several minutes (longer than a typical Bedrock token's or session's short-lived boundary) before submitting the answer, to start probing the B57 open question about AgentCore Memory short-term event expiry during an unbounded HITL wait.
- [ ] 5. Write the resume harness: start a **new** run against the same `thread_id`/`actor_id`, with the answer value injected the way a real `/ui-actions` resume would inject it (per `api.md`'s `ActionRequest` shape — `session_id`, `surface_id`, `revision`, `interaction_id`, `component_id`, `action_id`, `value`), and confirm the graph resumes past the interrupt with that answer visible to the next node.
- [ ] 6. Test a "stale" resume: submit an answer against an outdated `revision` and confirm the graph either rejects it or the test harness can at least detect the mismatch, informing the `stale_revision` error code's real enforcement point (`api.md`'s error table).
- [ ] 7. Push the wait duration further (hours, if practical within the spike's time budget, or research AgentCore Memory's documented short-term event expiry directly if a live multi-hour test is impractical) to narrow the B57 open item.
- [ ] 8. Write the findings note.

## Acceptance criteria

- A run that hits an `interrupt` ends the stream without `surface_end` and leaves a recoverable checkpoint.
- A new run resumes correctly with the injected answer, continuing the graph past the interrupt point.
- The findings note states, with evidence, whether AgentCore Memory short-term events have a maximum retention that would truncate an unbounded HITL wait, and if so, what that maximum is.
- The findings note has a pass/fail verdict for every item in "Spike questions to answer" below.

## Tests

- A pytest under `backend/agents/tests/` (or a spike-local test file) covering the spike graph's node logic and interrupt-payload shape in isolation, following the existing test patterns (e.g. `test_research_nodes.py`) already in the repo.
- The resume harness script itself is the integration-level verification; log exact checkpoint references and timestamps for the findings note.

## Spike questions to answer

Findings note: `.agents/phases/findings/S4.md`.

1. Does `AgentCoreMemorySaver` reliably checkpoint at an `interrupt` and allow a *new* run (not a resumed connection) to pick it up? Pass: confirms B56's core mechanism. Fail: B56 needs revisiting with the user before F13 starts.
2. Does AgentCore Memory short-term storage have a maximum event expiry that would silently drop a checkpoint during a long unbounded wait (B57)? Pass: no expiry within any realistic wait, or an expiry long enough to be a non-issue. Fail: B57's "no time limit" promise needs a mitigation (for example, a periodic checkpoint-refresh job) — this is the single most important open item this spike exists to resolve, per the decision log's "Still open" list.
3. Does a resume attempt against a stale `revision` fail safely (no double-execution, no corrupted state), or does it need explicit guarding in the production action endpoint? Pass/fail informs F13's `/ui-actions` validation logic.
4. Is the interrupt payload shape (the existing `ask_user_question` surface) sufficient to resume through the checkpoint unchanged, or does resuming need extra state not currently captured by the inline HITL tool? Pass/fail informs whether `shared/tools/hitl/inline_functions.py` needs changes before F13.

## Risks and open items

- B57's open item (AgentCore Memory short-term event expiry maximum) is explicitly called out in the decision log's "Still open" section and in `data-model.md` §9 and `security.md`'s open items — this spike is the primary place that question gets an evidence-based answer. If the spike finds a real expiry shorter than realistic HITL wait times, this is a finding that needs to go back to the user before F13 is designed, not something to quietly work around.
- This spike shares its checkpointer mechanism with S3 (both use `AgentCoreMemorySaver`); if S3 finds a different resume behavior for crash-recovery vs. what this spike finds for HITL-pause, reconcile the two findings notes before F5/F13 rely on either.

## Handoff notes

- F13 (HITL) wires the confirmed `interrupt`/checkpoint/resume mechanism into the real Research (clarifying question), Drafting (outline approval, revision loop), and Analysis (scope confirmation) HITL points named in B72, plus builds the real `/ui-actions` Lambda and production validation this spike only simulated with a harness script.
- If this spike finds a real AgentCore Memory expiry ceiling, F13's design and possibly B57 itself need to change — flag this clearly in the findings note so it reaches the user before F13 starts, per this roadmap's "how to use these files" rule 6.
