# F11 — Long-term memory
Status: partial (`CEIResearchMemory` has `user_preferences` and `semantic_facts` strategies; Research reads them; other agents not wired) | Depends on: F5 | Decisions: B28, B34, B36, B37, B57 | Specs: ../specs/agents.md, ../specs/data-model.md, ../specs/architecture.md

## Goal
Give the Research, Drafting, Analysis, and Pipeline Manager agents memory that lasts across chats. The agent should remember a user's stated preferences and useful facts about the user's work, without any explicit "remember" step and without a memory UI.

## Scope — In / Out

**In**
- Turn on the two AgentCore Memory long-term strategies: `user_preferences` and `semantic_facts` (B37).
- Per-user namespace (`actor_id = user_id`) shared across all of a user's chats (B34, data-model.md §9).
- Read relevant long-term memories into each specialist agent's context at the start of a turn.
- CDK resource for the AgentCore Memory long-term strategy configuration.

**Out**
- Any UI to view or edit long-term memories (B36 — no user UI in the MVP; see `later.md`).
- An explicit "remember this" tool. Corrections are extracted only by the two built-in strategies (B37).
- Long-term memory for the Tool Agent runtime (agents.md §5 says the tool agent has no cross-source memory; long-term strategies for it are not decided — treat as **Proposal (not decided)**).
- Deleting long-term memory (that is F19, data deletion).

## Deliverables
- `backend/lib/backend-stack.ts` — add the AgentCore Memory resource (or extend it if F5 already created one for the short-term checkpointer) with both long-term strategies configured, one per-user namespace pattern.
- `backend/agents/shared/memory/agentcore.py` — exists (partial: `create_checkpointer`, `create_memory_store`, `build_invocation_config`). Add:
  - `fetch_long_term_context(store, user_id) -> dict` — reads `user_preferences` and `semantic_facts` for a user's namespace.
  - Confirm `build_invocation_config` carries the `actor_id` the long-term strategies need (already does — reuse, do not duplicate).
- `backend/agents/shared/config.py` — add `memory_id: str` (the AgentCore Memory resource ID), read from environment, matching the `CEI_CODE_INTERPRETER_ID` pattern already used for the Code Interpreter.
- `backend/agents/research/nodes.py`, `backend/agents/drafting/nodes.py` — add a small "load memory" step before the first model call that calls `fetch_long_term_context` and folds the result into the system prompt input (via `shared/prompts/loader.py`, which exists).
- `backend/agents/analysis/nodes.py`, `backend/agents/pipeline_manager/nodes.py` — same wiring, done together with F14/F17 since those node bodies do not exist yet (see "Handoff notes").

## Tasks
- [ ] 1. Add the AgentCore Memory CDK construct (or extend F5's) with `user_preferences` and `semantic_facts` long-term strategies, namespace template `/users/{actorId}`.
- [ ] 2. Add `memory_id` to `shared/config.py` and wire it through `deploy.sh`/CDK outputs (matching the `CodeInterpreterId` CfnOutput pattern in `backend-stack.ts`).
- [ ] 3. Implement `fetch_long_term_context` in `shared/memory/agentcore.py` using `AgentCoreMemoryStore.get`/`search` (research the exact SDK call against `langgraph_checkpoint_aws`).
- [ ] 4. Wire `fetch_long_term_context` into Research's entrypoint (`backend/agents/main.py`) before `graph.ainvoke`, passing the result into `initial_state` for the prompt loader to use.
- [ ] 5. Wire the same call into Drafting's entrypoint.
- [ ] 6. Add a unit test double (`FakeMemoryStore`) so agent unit tests do not need live AWS credentials.
- [ ] 7. Confirm (research spike, not a code task) whether AgentCore Memory extraction of `user_preferences`/`semantic_facts` runs automatically per turn or needs an explicit `create_event` call after each turn — implement whichever the SDK requires.
- [ ] 8. Document the namespace convention and the "no explicit remember tool" rule in `docs/architectureDeepDive.md` under an ADR entry.

## Acceptance criteria
- A second chat, in a new session, for the same user, reflects a preference stated in an earlier chat (for example, a preferred citation style), without the user restating it.
- No code path lets a user read or edit another user's long-term memory (namespace is strictly `actor_id = user_id`).
- No tool named "remember" or equivalent exists anywhere in the tool registry.
- Turning long-term memory off (missing `memory_id`) does not crash the agent; it runs with no long-term context, degrading gracefully.

## Tests
- Unit: `fetch_long_term_context` against a `FakeMemoryStore`, empty-namespace case, populated case.
- Unit: Research/Drafting entrypoint tests confirming the prompt loader receives the fetched context.
- CDK assertion test: the Memory construct has both strategies configured with the correct namespace template; cdk-nag passes or carries an ADR-format suppression.
- Integration (manual/local, since AgentCore Memory needs live AWS): confirm a fact stated in chat A appears in chat B's context after the AgentCore Memory extraction window.

## Risks and open items
- AgentCore Memory short-term event expiry maximum is still unverified (B57, open item in decision log and in `data-model.md`/`agents.md`). It does not block this phase (that item is about short-term HITL waits), but the same SDK research should confirm long-term extraction timing while it is being done.
- **Proposal (not decided):** whether the Tool Agent runtime gets long-term memory at all (agents.md §5 implies no cross-source memory, but does not explicitly rule out long-term strategies). Flagged for F15 to resolve when the Tool Agent runtime is built.
- Analysis and Pipeline Manager wiring (tasks not listed above) is deferred to F14 and F17, since those agents' node bodies do not exist yet — see Handoff notes.

## Handoff notes
- F14 (Analysis agent) and F17 (Pipeline Manager) must call `fetch_long_term_context` the same way Research/Drafting do, once their node bodies exist. This phase only guarantees the helper function and the CDK resource exist.
- F19 (data deletion) depends on this phase's namespace convention (`actor_id = user_id`) to delete a user's long-term memory on account delete.
