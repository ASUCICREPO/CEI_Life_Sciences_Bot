# S3 — Async spike

Status: not started | Depends on: — | Decisions: B25, B28 | Specs: [`../specs/data-model.md`](../specs/data-model.md) §3 (Job ID Store), §9 (AgentCore Memory), [`../specs/agents.md`](../specs/agents.md) (async entrypoint pattern, §1/§6), [`../specs/architecture.md`](../specs/architecture.md) §2.2

## Goal

Prove that an AgentCore Runtime async session can run for more than one hour, report `HealthyBusy` while busy, be cancelled cooperatively plus with `StopRuntimeSession`, and resume correctly from an `AgentCoreMemorySaver` checkpoint after the session is killed. This spike de-risks B25 (Fargate removed; background jobs are AgentCore Runtime async sessions, max 8h/2vCPU/8GB) before F16 builds the real Job ID Store and watchdog on top of it.

## Scope — In / Out

**In:** a minimal AgentCore Runtime deployment of a trivial "long task" graph (a LangGraph with a few nodes and a sleep/loop step to simulate long work), using the real `add_async_task`/`complete_async_task` pattern, `AgentCoreMemorySaver` as its checkpointer, and manual/scripted triggering of cancel and kill scenarios.

**Out:** the real Research/Drafting/Analysis/Pipeline Manager graphs, the real Job ID Store table and Polling Lambda (F16 builds these; this spike can use a throwaway DynamoDB table or just log status to CloudWatch), the watchdog Lambda itself (F16) — this spike proves the runtime-side mechanics the watchdog will later rely on.

## Deliverables

- `backend/agents/spikes/async_spike/` (new, Python — a minimal LangGraph app with `graph.py`, `nodes.py`, a `main.py` entrypoint using `add_async_task`/`complete_async_task` and `AgentCoreMemorySaver` from `backend/agents/shared/memory/agentcore.py`)
- A trivial CDK addition (or manual AgentCore CLI deployment, per B46 "developers use local AgentCore CLI runs") — spike-only, not merged into `backend-stack.ts`
- A findings note at `.agents/phases/findings/S3.md`

## Tasks

- [ ] 1. Wire `backend/agents/shared/memory/agentcore.py`'s `create_checkpointer`/`build_invocation_config` into the spike graph (this is the first real usage of this existing-but-unwired code in the repo).
- [ ] 2. Write a spike graph with a node that loops, sleeping and checking a cooperative `cancel_requested` flag between iterations, simulating a task that would run over an hour in production (compress the actual sleep duration for practical testing, but structure the loop identically to how a real long task would check the flag).
- [ ] 3. Deploy the spike graph to AgentCore Runtime as an async session; confirm `add_async_task` at start and `complete_async_task` at the end behave as documented, and that the runtime reports `HealthyBusy` via the ping mechanism while the loop runs.
- [ ] 4. Run the session past the compressed-equivalent of one hour (or, if AgentCore allows, run a real ~65-minute session) and confirm it does not time out unexpectedly before the 8-hour cap.
- [ ] 5. Test cooperative cancel: set `cancel_requested` externally (write directly to whatever throwaway store the spike uses) and confirm the graph observes it between nodes and exits cleanly.
- [ ] 6. Test hard cancel: call `StopRuntimeSession` directly against a running spike session and confirm the session stops.
- [ ] 7. Test crash resume: kill the spike session mid-run (via `StopRuntimeSession` or a forced Lambda/runtime error) after at least one checkpoint has been written, then start a new session against the same `thread_id`/`actor_id` and confirm `AgentCoreMemorySaver` resumes from the last checkpoint rather than restarting from scratch.
- [ ] 8. Test token expiry during a long run (only if S2 has landed a stub refresh endpoint; otherwise note this as carried into S2/F4's scope) — confirm the async session can survive a 1-hour token boundary without the whole run failing.
- [ ] 9. Confirm behavior during a redeploy: start a session, redeploy the AgentCore Runtime artifact mid-run, and record what happens to the in-flight session (this is one of B25's explicitly named prototype items).
- [ ] 10. Write the findings note.

## Acceptance criteria

- A spike session runs past the equivalent of one hour without an unexpected timeout.
- `HealthyBusy` pings are observed while the loop runs.
- Cooperative cancel and `StopRuntimeSession` both stop a running session.
- A new session resumes from the last `AgentCoreMemorySaver` checkpoint after the prior session is killed, without repeating already-completed work.
- The findings note has a pass/fail verdict for every item in "Spike questions to answer" below, including the redeploy behavior.

## Tests

- No unit tests in the traditional sense (infrastructure/runtime behavior spike). The spike graph itself should have a small pytest covering its node logic (cancel-flag check, checkpoint write points) in isolation, reusing the existing test patterns under `backend/agents/tests/`.
- Record exact timestamps and session IDs for each scenario run in the findings note so results are reproducible/auditable.

## Spike questions to answer

Findings note: `.agents/phases/findings/S3.md`.

1. Does an AgentCore async session reliably exceed 1 hour without AWS-side termination, up toward the documented 8-hour cap? Pass/fail directly informs F16's watchdog staleness thresholds.
2. Does `AgentCoreMemorySaver` resume produce the same state as if the run had never stopped, for a simple graph? Pass: confirms B28's crash-resume assumption. Fail: means F16/F5's checkpoint strategy needs redesign before background jobs ship.
3. What is the practical maximum AgentCore Memory short-term event expiry, and does it interact with a long-running async session's own checkpoints (separate from the HITL-wait question in B57, but related)? This spike answers it for the async-run case; S4 answers it for the HITL-wait case.
4. What exactly happens to an in-flight async session during a redeploy of its runtime artifact? Pass/fail informs whether F16's watchdog needs a "redeploy-safe" resume path, or whether `deploy.sh` needs a drain step before redeploying agent runtimes.
5. Does token expiry (1h, per B67) actually interrupt a long-running session's tool calls if no refresh is attempted, confirming the refresh endpoint in B67 is necessary rather than optional? Pass: confirms B67 is required. Fail (i.e., tokens turn out not to expire mid-session for some technical reason): flag for the user, do not silently drop B67.

## Risks and open items

- AgentCore Memory short-term event expiry maximum is an explicit open item in the decision log (see "Still open" in `decision-log.md` and B57's note in `data-model.md` §9) — this spike should narrow it experimentally even though the final answer may need AWS support/docs confirmation beyond what a spike can observe.
- This spike depends on S2 only loosely (task 8); if S2 has not produced a stub refresh endpoint yet, task 8 can be deferred to F4/F16 without blocking the rest of this spike.
- Redeploy behavior (task 9) has real production-safety implications for F1's `deploy.sh` — if a redeploy silently kills in-flight background jobs with no resume path, that is a gap worth raising to the user before F16 locks in its design.

## Handoff notes

- F16 (Background jobs) builds the real Job ID Store, Polling Lambda, and watchdog directly on this spike's confirmed mechanics (heartbeat cadence, resume behavior, cancel pattern). F16's watchdog staleness threshold should be set using this spike's observed heartbeat timing, not guessed.
- F5, F7, F14, F17 (the four agent runtimes) all eventually need the async entrypoint pattern (`add_async_task`/`complete_async_task`, cooperative cancel) proven here; F5 is the first to wire it for real (once B23's async threshold is decided).
- If the redeploy-safety question (task 9) surfaces a real gap, note it for F1's `deploy.sh` design and for F21 (hardening) rather than silently absorbing it into F16 alone.
