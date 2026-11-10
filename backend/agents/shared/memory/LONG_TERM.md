# Long-term memory integration

`long_term.py` is an isolated helper; no agent entrypoint currently calls it and
no deployed extraction or cross-chat retrieval has been proven. It reads the
exact `/users/{actorId}/` namespace. The namespace comes only from the verified
Cognito subject; never allow an agent or tool argument to select an actor.

AgentCore Memory's built-in semantic and user-preference strategies process
conversational events asynchronously. The installed `AgentCoreMemorySaver`
stores checkpoints as blob events, which AgentCore does not extract into
long-term memory. Wiring the checkpointer alone is therefore insufficient:
record each completed USER/ASSISTANT turn with a stable idempotency token using
`record_conversation` (or prove an equivalent event writer already exists).
Only supply user-visible conversation text, not secrets, tool payloads or traces.
Sources: [memory strategies](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-strategies.html),
[event payload types](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/short-term-create-event.html).

Before integration, configure the two approved built-in strategies with the
same `/users/{actorId}/` namespace on the CDK Memory resource, pass the memory ID
to specialist runtimes, and scope their permissions. Confirm this namespace and
retrieval behavior in a real two-user deployment; built-in defaults differ.
Research, Drafting, Analysis and Pipeline must retrieve relevant bounded records
at turn start using the authenticated user and current message. Treat memory as
untrusted context, not system instructions. Do not add a memory UI or tool-agent
memory without a decision.

Fence event writes and extraction during deletion. Implement account cleanup of
both memory strategies and ensure background extraction cannot recreate records
after deletion; until that is proven, account deletion must not report complete.
Add integration tests for cross-chat recall, extraction delay, cross-user
isolation, retries/idempotency, deletion races, no-memory configuration, and
unavailable service behavior. Missing configuration returns no context; service
errors propagate so callers can apply an explicit availability policy.
