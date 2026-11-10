# F17 — Pipeline Manager
Status: not started | Depends on: F13, F16 | Decisions: B23/B79, B25, B54, B56, B71, B72, B83 | Specs: ../specs/agents.md, ../specs/architecture.md, ../specs/api.md

## Goal
Implement the Pipeline Manager: it plans a multi-step task across Research/Drafting/Analysis, gets the plan approved by the user in chat, runs the approved plan as an always-async job, and shows live step status on `/pipeline`.

## Scope — In / Out

**In**
- Full implementation of `plan`, `approve`, `dispatch`, `monitor`, `synthesize` in `backend/agents/pipeline_manager/nodes.py` (today all five `raise NotImplementedError`; `should_continue` is already implemented and stays as-is).
- Plan approval as a real HITL pause, via `request_plan_approval` and F13's `interrupt` mechanism (B54, B56).
- Always-async execution — Pipeline Manager runs never use the SSE real-time path (B23/B79); every run uses F16's AgentCore async-session mechanism from the start.
- Each dispatched step calls the target specialist agent's own AgentCore Runtime directly (`InvokeAgentRuntime`), not in-process — concurrent steps run in separate sessions, each with its own 2 vCPU/8 GB allocation, and each gets its own trace (B83).
- The `/pipeline` page (ported from the founder-ai-dashboard prototype, `PipelinePage.tsx`) wired to poll `GET /jobs/{jobId}` for live step status, per B54 ("No visual builder... `/pipeline` shows the live step status").
- Self-healing within the existing `should_continue` retry budget (up to 3 retries per failed step) — no new retry logic needed, just real steps to retry.

**Out**
- A visual pipeline builder (B54 explicitly rules this out — the user always asks in chat, the Pipeline Manager proposes the plan).
- Any change to Research/Drafting/Analysis's own logic — Pipeline Manager calls them as-is.
- Slides (PPTX) export (deferred, see `later.md`).

## Deliverables
- `backend/agents/pipeline_manager/state.py` — exists (`PipelinePlan`, `PipelineStep`, `StepStatus`, `PipelineState`). No structural change expected; confirm `PipelineState.plan_approved` is set only through the `approve` node's interrupt resume, never by a plain field write.
- `backend/agents/pipeline_manager/nodes.py` — implement all five stub functions.
- `backend/agents/pipeline_manager/graph.py` — exists and already wires `plan → approve →(cond)→ [dispatch ⇄ monitor loop]→ synthesize → END`; no structural change needed, only the node bodies.
- `backend/agents/pipeline_manager/main.py` (new) — AgentCore Runtime entrypoint using F16's async-session pattern from the start (unlike Research/Drafting/Analysis, which start synchronous and convert later).
- `frontend/src/pages/PipelinePage.tsx` (once the dashboard is ported into `frontend/` per B1/B66, in F2) — replace the hardcoded `pipelineSteps` array and the local `approved` state with live data: `GET /jobs/{jobId}` for step status, `POST /ui-actions` for the plan-approval `ReviewGate` (the page already renders a `ReviewGate`/`ExportCard`/`TaskProgress` shape that matches this contract closely; remove the "Sample data for demonstration" notice and the hardcoded step list).
- `backend/lib/backend-stack.ts` — add the Pipeline Manager AgentCore Runtime construct.

## Tasks
- [ ] 1. Implement `plan`: produce a `PipelinePlan` (summary, ordered `PipelineStep`s with `agent`/`description`/`dependencies`, an estimated duration).
- [ ] 2. Implement `approve`: build a `ReviewGate` surface from the plan (reuse `request_plan_approval`'s existing surface-building code in `shared/tools/hitl/inline_functions.py`) and call it through F13's `interrupt` mechanism; set `plan_approved` from the resumed value.
- [ ] 3. Implement `dispatch` per B83: for each ready step (dependencies satisfied), call the named sub-agent's (Research/Drafting/Analysis) own AgentCore Runtime directly via `InvokeAgentRuntime` — not in-process (this replaces the earlier in-process default this file used to propose, which is now superseded by B83). Run concurrent steps in separate sessions, each with its own 2 vCPU/8 GB allocation and its own trace. Pipeline Manager, as a specialist agent (B26/B30), already holds all source scopes, so each dispatched call can use Pipeline Manager's own token; no separate Router round-trip is needed per step, but confirm this in testing rather than assuming it.
- [ ] 4. Implement `monitor`: check each running step's status, update `StepStatus`, surface partial results per step.
- [ ] 5. Implement `synthesize`: combine all completed/partial step results into one unified output.
- [ ] 6. Add `pipeline_manager/main.py` using F16's async-session entrypoint pattern from the start.
- [ ] 7. Add the Pipeline Manager Runtime construct to `backend-stack.ts`.
- [ ] 8. Wire `PipelinePage.tsx` to `GET /jobs/{jobId}` polling and `POST /ui-actions` for plan approval, once the frontend port exists.

## Acceptance criteria
- A multi-step request produces a plan, pauses for approval, and — once approved — runs every step as one async job (never over SSE).
- A step that fails is retried up to 3 times (existing `should_continue` logic) before the pipeline moves to `synthesize` with a `partial` status noted for that step.
- `GET /jobs/{jobId}` reflects real step-by-step progress that `/pipeline` can render without any mock data.
- Rejecting the plan at the `approve` HITL point ends the run without dispatching any step.
- Two concurrent steps in the same pipeline run as two separate AgentCore Runtime sessions, each with its own trace, and neither step's trace is mixed into the other's (B83).

## Tests
- Unit (pytest): `plan` output shape; `approve` interrupt/resume (approved and rejected paths); `dispatch` step-readiness logic (dependency graph) and its `InvokeAgentRuntime` call per step (mocked, asserting no in-process call is made, per B83); `monitor` status aggregation; `synthesize` combination logic.
- Unit: `should_continue` routing still correct against the newly-real `steps` data (regression test, since this function is unchanged but now receives real data instead of never being reached).
- CDK assertion + cdk-nag: Pipeline Manager Runtime construct.
- Integration: a full plan → approve → dispatch (mocked sub-agents) → monitor → synthesize run, including one seeded step failure that retries and one that stays `partial`.
- Frontend (vitest, once ported): `PipelinePage.tsx` renders live job data instead of the mock `pipelineSteps` array; plan-approval button calls the real action endpoint.

## Risks and open items
- **Cross-phase dependency note:** this phase depends on F13 and F16 per the decision-log Phase list, which this file's header matches — no gap found here (unlike F14's earlier, now-resolved, header gap).
- **Resolved by B83:** the `dispatch` mechanism is no longer an open question. Unlike Drafting's `research_subagent` (B71: in-process, does not call the Research runtime), Pipeline Manager's dispatch is the opposite pattern — it always calls the target specialist's own AgentCore Runtime (`InvokeAgentRuntime`), never in-process. Do not reuse Drafting's in-process pattern here.
- Frontend wiring (task 8) cannot start until F2 has ported the founder-ai-dashboard into `frontend/`; if F2 has not landed by the time this phase's backend work is done, ship the backend pieces first and hold the frontend task.

## MVP slice (B94, B95, B96, B98)
Built before F13/F16 for the client demo, local only. In scope: all five nodes, plan approval as a real `interrupt()`, in-process dispatch behind a `Dispatcher` interface (sequential and concurrent waves from the dependency graph), a background `asyncio` task registered with `add_async_task`, an in-memory `JobRegistry`, `op: "job_status"` with `steps[]` and a CEI UI status surface, `pipeline_main.py` on port 8084, replay mode, and `/pipeline?job=<id>` polling in the Vite dashboard. Still left for the full phase: B83 runtime dispatch, F16 Job ID Store/Polling Lambda/watchdog, the CDK runtime construct.

## Implementation status (2026-11-10, B102)
B102 keeps the in-process `InProcessDispatcher` for this iteration; task 3's `InvokeAgentRuntime` dispatch and the separate-session acceptance criterion are deferred. Built on `complete-cei-workflows` (statically reviewed; pytest/jest and deployment pending in CodeBuild):
- `pipeline_main.py` uses the durable Job ID Store when `CEI_JOB_TABLE` is set (`shared/runtime/durable_jobs.py`), else the local registry. Creating a job takes the user's single background slot before the approval is consumed, so a `concurrency_limit` refusal leaves the plan pending.
- A 60-second heartbeat keeps the watchdog away and cancels the in-process run when the user cancels the job; a late completion after cancellation is ignored by a conditional write.
- The Pipeline runtime has `CEI_JOB_TABLE`/`CEI_ACTIVE_RUNS_TABLE` and exact table actions (`AgentRuntimes.configureJobStore`).
Still open: `/pipeline` without `?job=` still shows sample data (task 8 for the job list), and durable checkpoints for the in-process steps depend on S4.

## Handoff notes
- F20 (Alarms) should *not* add a Pipeline-Manager-specific "stuck pipeline" alarm — B75 explicitly excludes agent-health alarms in favor of the watchdog (F16) and the dashboard (`/pipeline` itself).
- F19 (data deletion) must be able to cancel and clean up an in-flight Pipeline Manager job as part of an account-delete cascade — coordinate the job's `session_id` linkage so F19 can find it via the Job ID Store.
