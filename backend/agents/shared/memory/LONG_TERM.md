# Long-term memory integration

Research has a retrieval path using `long_term.py` and `turns.py`; it is off by
default and requires both `CEI_RESEARCH_LONG_TERM_ENABLED=true` and
`CEI_RESEARCH_MEMORY_ID`. Do not enable it in a deployment before live proof
and deletion recovery are complete. The authenticated Cognito subject
determines the exact `/users/{actorId}/` namespace; agent and tool arguments do
not select an actor. Research treats the retrieved records as untrusted user
context, not system instructions. No deployed extraction, cross-chat retrieval,
or cross-user isolation has been proven.

AgentCore Memory's built-in semantic and user-preference strategies process
conversational events asynchronously. The installed `AgentCoreMemorySaver`
stores checkpoints as blob events, which AgentCore does not extract into
long-term memory. Wiring the checkpointer alone is therefore insufficient:
record each completed USER/ASSISTANT turn with a stable idempotency token using
`record_conversation` (or prove an equivalent event writer already exists).
Only supply user-visible conversation text, not secrets, tool payloads or traces.
Sources: [memory strategies](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-strategies.html),
[event payload types](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/short-term-create-event.html).

The CDK Research Memory resource configures the two approved built-in
strategies with the same namespace, and the Research role is granted retrieval
on that resource. Confirm the namespace and retrieval behavior in a real
two-user deployment; built-in defaults differ. Drafting, Analysis and Pipeline
still need equivalent retrieval. Do not add a memory UI or tool-agent memory
without a decision.

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
