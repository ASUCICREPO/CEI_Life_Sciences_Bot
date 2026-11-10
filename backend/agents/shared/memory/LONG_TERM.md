# Long-term memory integration

Research, Drafting, Analysis and Pipeline Manager each have a retrieval path
through the generic `shared/memory/turns.load_long_term_context`; every path is
off by default and requires both an agent-specific `CEI_<AGENT>_LONG_TERM_ENABLED=true`
flag and a `CEI_<AGENT>_MEMORY_ID`: `CEI_RESEARCH_LONG_TERM_ENABLED`/`CEI_RESEARCH_MEMORY_ID`,
`CEI_DRAFTING_LONG_TERM_ENABLED`/`CEI_DRAFTING_MEMORY_ID`,
`CEI_ANALYSIS_LONG_TERM_ENABLED`/`CEI_ANALYSIS_MEMORY_ID`,
`CEI_PIPELINE_LONG_TERM_ENABLED`/`CEI_PIPELINE_MEMORY_ID`. Do not enable any of
them in a deployment before live proof and deletion recovery are complete. The
authenticated Cognito subject determines the exact `/users/{actorId}/`
namespace; agent and tool arguments do not select an actor. Every agent treats
the retrieved records as untrusted user context, not system instructions
(`render_untrusted_memories`, plus an "untrusted data" system line, in each
agent's first model call and its final writing call). No deployed extraction,
cross-chat retrieval, or cross-user isolation has been proven.

Each runtime recalls at most once per new run, before `begin_turn`; a resumed
HITL turn never re-recalls. Pipeline Manager loads its context once and keeps
it in graph state, so a resumed background job reuses the checkpointed value.
In-process sub-runs (`pipeline_manager/dispatcher.py`'s research/drafting
steps, and Drafting's `research_subagent`) receive the parent's already-loaded
context in their initial state and never call memory themselves. An analysis
step dispatched by the Pipeline Manager is the one exception: `analysis/run.py`
is outside this phase's file list and does not yet accept `long_term_context`,
so that path does not receive the parent's recalled context — tracked here as
a known gap, not a decision to skip it permanently. Recall-only: no agent calls
`record_completed_turn`; writes remain gated on the deletion-fencing work below.

AgentCore Memory's built-in semantic and user-preference strategies process
conversational events asynchronously. The installed `AgentCoreMemorySaver`
stores checkpoints as blob events, which AgentCore does not extract into
long-term memory. Wiring the checkpointer alone is therefore insufficient:
record each completed USER/ASSISTANT turn with a stable idempotency token using
`record_conversation` (or prove an equivalent event writer already exists).
Only supply user-visible conversation text, not secrets, tool payloads or traces.
Sources: [memory strategies](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-strategies.html),
[event payload types](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/short-term-create-event.html).

The CDK Memory resource (logical id `ResearchMemory`, one resource shared by
all four runtimes) configures the two approved built-in strategies with the
same namespace. All four runtime roles are granted `RetrieveMemoryRecords` on
it; only Research is also granted `CreateEvent`/`ListEvents`/`DeleteEvent`,
since it is the only agent with an event-writing path today (still unused —
see below). Confirm the namespace and retrieval behavior in a real two-user
deployment; built-in defaults differ. Do not add a memory UI or tool-agent
memory without a decision.

`turns.record_completed_turn` requires a writer fence and an idempotency token,
but no agent calls it: the fence is not deployed and a successful CreateEvent
can trigger extraction after the writer releases its token. Fence event writes
and extraction during deletion. Implement account cleanup of
both memory strategies and ensure background extraction cannot recreate records
after deletion; until that is proven, account deletion must not report complete.
Add integration tests for cross-chat recall, extraction delay, cross-user
isolation, retries/idempotency, deletion races, no-memory configuration, and
unavailable service behavior. Missing configuration returns no context; service
errors propagate so callers can apply an explicit availability policy.
