# F16 — Background jobs
Status: not started | Depends on: F5, S3 | Decisions: B22, B23/B79, B24, B25, B28, B67 | Specs: ../specs/architecture.md, ../specs/data-model.md, ../specs/api.md

## Goal
Let a run that will take more than 15 minutes finish as a background job instead of over SSE: an AgentCore Runtime **async session** (not Fargate — B25), tracked in a Job ID Store, checked by a Polling Lambda, and recovered by a watchdog if it goes stale.

## Scope — In / Out

**In**
- Job ID Store (DynamoDB), per `data-model.md` §3.
- Polling Lambda: `GET /jobs`, `GET /jobs/{jobId}`, `POST /jobs/{jobId}/cancel` (per `api.md`).
- A watchdog (scheduled Lambda) that marks stale jobs failed (based on `heartbeat_at`) and can resume from the last checkpoint.
- Rewriting `shared/transport/async_task.py` (currently an ECS-shaped stub, predating B25) to target AgentCore Runtime async sessions: `add_async_task`/`complete_async_task`, `HealthyBusy` pings, cooperative cancel flag + `StopRuntimeSession`.
- The async-vs-real-time decision rule (B23/B79): Pipeline Manager runs are always async; the other three agents go async when a plan/triage step's duration estimate crosses 10 minutes (600s), leaving a 5-minute margin under the 15-minute stream cap. This decision is made once, before a run starts; there is no mid-run handoff from streaming to async.
- The per-user concurrency counters from B22 (2 real-time, 1 background) as they relate to starting a background job (the counters table itself may be delivered by F5; this phase adds the "1 active background run" enforcement point).
- Token refresh for long-running sessions (B67): the running agent calls an internal token-refresh endpoint before its Gateway token expires.

**Out**
- The Pipeline Manager's own node logic (F17) — this phase only makes the async session mechanism available; Pipeline Manager is simply the one agent type that always uses it.
- The `/pipeline` live-step-status page (F17).
- HITL pause/resume mechanics themselves (F13) — this phase's job is only to keep a paused async session alive/resumable, reusing F13's checkpoint, not to build the pause logic.

## Deliverables
- `backend/agents/shared/transport/async_task.py` — exists (partial: `TaskResult`, `TaskStatus`, `AsyncTaskClient` with `submit`/`poll`/`cancel` all `NotImplementedError`, built against `ecs` boto3 client). Rewrite: drop the `ecs` client and `cluster`/`task_definition`/`subnets` parameters (Fargate-shaped, removed by B25); replace with AgentCore Runtime async-session calls (`bedrock-agentcore` client: start an async invocation, poll `HealthyBusy`/completion status, call `StopRuntimeSession` for cancel).
- `backend/lambdas/polling/handler.py` (new) — implements the three Jobs routes from `api.md`.
- `backend/lambdas/watchdog/handler.py` (new) — EventBridge-scheduled; scans the Job ID Store's `GSI-1` (`status = running`, stale `heartbeat_at`) and marks stale jobs failed; attempts resume from `checkpoint_ref` where possible.
- `backend/lambdas/token-refresh/handler.py` (new) — internal, IAM-authenticated endpoint per B67: checks the job/run is still active (Job ID Store or an active-run record), then returns a new token with the same scopes and user, without ever exposing the M2M secret to the caller.
- `backend/lib/backend-stack.ts` — add: the Job ID Store table (with `GSI-1`), the Polling Lambda + its three routes, the watchdog Lambda + EventBridge rule, the token-refresh Lambda + IAM authorizer, and IAM permissions for `StopRuntimeSession` scoped to the calling user's own sessions only.
- `backend/agents/shared/config.py` — add `async_threshold_seconds: int`, defaulted to `600` (10 minutes, B79's decided value — not a placeholder; keep it as a config knob so it can be tuned later, but the default is the real decision).
- `backend/agents/research/nodes.py` (and Drafting's, once each agent's `triage`/planning step exists) — add a duration estimate to the triage/plan output (for example, a new `estimated_duration_seconds` field on `TriagePlan`) that the entrypoint uses to decide sync vs. async.

## Tasks
- [ ] 1. Design the Job ID Store schema exactly as `data-model.md` §3 specifies; add the CDK table + `GSI-1`.
- [ ] 2. Rewrite `AsyncTaskClient` against the AgentCore Runtime async-session API (research the exact `bedrock-agentcore` boto3 calls for `add_async_task`/`complete_async_task`/`HealthyBusy`/`StopRuntimeSession`).
- [ ] 3. Add the cooperative cancel flag check between LangGraph node executions (a small helper each async-capable agent's entrypoint calls between steps).
- [ ] 4. Build the Polling Lambda (`GET /jobs`, `GET /jobs/{jobId}`, `POST /jobs/{jobId}/cancel`).
- [ ] 5. Build the watchdog Lambda and its EventBridge schedule.
- [ ] 6. Build the token-refresh Lambda per B67 (IAM-authenticated, checks run/job is active, returns a same-scope token).
- [ ] 7. Add the `async_threshold_seconds` config value (default `600`, B79) and the triage-step duration estimate field.
- [ ] 8. Wire the entrypoint decision: if the estimate exceeds the threshold (or the agent is Pipeline Manager), start an async session and return a `job_handoff`-shaped response instead of streaming to completion (coordinate with F6's `job_handoff` SSE event). This decision is made once, before the run starts streaming — do not build any path that switches an already-streaming run to async mid-run (B79 explicitly rules this out).
- [ ] 9. Add the "1 active background run per user" concurrency check (Active-run counters table, per `data-model.md` §6) before starting a job.

## Acceptance criteria
- A Research run whose triage step estimates a duration above the threshold starts an AgentCore async session instead of streaming, and `GET /jobs/{jobId}` shows its status transitioning `pending → running → completed`.
- Killing the watchdog's target job's heartbeat (test double) causes the watchdog to mark it `failed` within one scheduled run.
- `POST /jobs/{jobId}/cancel` sets `cancel_requested` and the running session stops within one node boundary check, calling `StopRuntimeSession`.
- A token nearing its 1-hour TTL (B67) during a long job is refreshed automatically without the agent ever holding the M2M secret.
- A user already running 1 background job cannot start a second one (returns `concurrency_limit`, per `api.md`'s error table).

## Tests
- Unit (pytest): `AsyncTaskClient.submit`/`poll`/`cancel` against a mocked `bedrock-agentcore` client.
- Unit: watchdog stale-detection logic (heartbeat older than N minutes → `failed`).
- Unit: token-refresh handler — active job → new token; inactive/unknown job → rejected.
- Unit: Polling Lambda routes — happy path and each error case in `api.md`'s error table.
- CDK assertion + cdk-nag: Job ID Store schema (including `GSI-1`), watchdog schedule, token-refresh Lambda's IAM authorizer, `StopRuntimeSession` IAM scoping.
- Integration: submit a job, poll to completion, cancel a different job mid-run, and confirm the watchdog does not touch a healthy job.

## Risks and open items
- B79 fixes the async threshold at 10 minutes (600s) — this phase ships the real decided value as a config default, not a placeholder. Keep it configurable in case it needs tuning later, but do not present it as still-undecided.
- **Active-run counter leak recovery** is a `data-model.md` open item (a crash between increment and decrement never frees a slot) — this phase's watchdog is the natural place to also repair counters when it marks a job stale, per that file's own suggestion; implement this as part of task 9, not as an afterthought.
- Token TTL vs. long async sessions (up to 8h) is flagged in `auth-and-tool-scope.md` §8 as unresolved beyond B67's basic refresh mechanism — B67 covers the common case (refresh before expiry); this phase should confirm the refresh endpoint is called on a schedule that comfortably beats the 1-hour TTL, but the exact refresh cadence is a **Proposal (not decided)** — propose refreshing at 45 minutes into a 60-minute token life.
- Long HITL waits inside an async job (checkpoint, stop session, resume in a new session after approval, per B25) depend on F13's checkpoint mechanism; if F13 has not landed, this phase can build the async-session plumbing but cannot fully test the pause-resume-within-async-job path end to end.

## Implementation status (2026-11-10)
Built on `complete-cei-workflows` (statically reviewed; tests and deployment pending in CodeBuild):
- Job ID Store `shared/persistence/jobs.py` and CDK `constructs/background-jobs.ts`: PK `user_id`, SK `job_id` (time-ordered `job_<ms>_<hex>`), KEYS_ONLY `status-updated_at` index. Every transition is conditional; terminal jobs never change; cancellation beats a late completion; step updates write only their own step; `progress_pct` is derived on read.
- Polling Lambda `lambdas/jobs-handler` with `GET /jobs`, `GET /jobs/{jobId}`, `POST /jobs/{jobId}/cancel`. Cancel sets the flag, makes a best-effort `StopRuntimeSession` with the caller's own bearer token (the runtimes accept only SPA user tokens), finalizes `cancelled` and frees the slot.
- Watchdog `lambdas/watchdog` every 5 minutes: fails running jobs with no write for 600 s and pending jobs older than 3600 s, conditional on no newer write, and frees their slot (the counter-leak repair). Checkpoint requeue is coded but disabled (`CEI_WATCHDOG_RESUME_ENABLED=false`, B102).
- Single background slot (`ActiveRunRepository.acquire_background`, 9-hour lease).
- Token refresh accepts `job_id` + `user_id`: authorized by the runtime role allowlist and a running, uncancelled job for that user and session; scopes come from the job record.
Still open: the `async_task.py` rewrite and the 600 s estimate handoff for Research/Drafting/Analysis (B79/B100), and every S3 proof (one-hour run, HealthyBusy, hard cancel, recovery, redeploy). The `StopRuntimeSession` data-plane path `/runtimes/{arn}/stopruntimesession` must be confirmed live.

## Handoff notes
- F17 (Pipeline Manager) is the primary consumer of this phase's "always async" path and the `/jobs` polling contract that its `/pipeline` UI will read from.
- F19 (data deletion) must be able to find and stop any in-flight job for a user being deleted — coordinate the Job ID Store's `user_id` PK with F19's account-delete cascade.
- F20 (alarms) should alarm on Job ID Store DynamoDB throttling (already in B75's fixed list) but explicitly should **not** add a separate "stale job" alarm — B75 says the watchdog and dashboard handle that, not an SNS alarm.
