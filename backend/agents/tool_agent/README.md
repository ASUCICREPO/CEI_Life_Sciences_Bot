# Single-source tool agent

This package binds exactly one of the 12 existing data sources to each graph.
The selected source is included in the checkpoint thread ID, and every deployed
request requires a verified Gateway credential bound to the session and user,
with exactly that source scope and `cei-tools/common`. No token is checkpointed.

The `ncbi` profile is reserved but intentionally unavailable until taxonomy,
sequence and BLAST targets exist. The runtime, streaming route, source-scoped
token minting, Guardrails, NCBI tools, common tools, and infrastructure wiring
must be integrated and verified before deployment. Use the AgentCore Memory ID
environment variable `CEI_TOOL_AGENT_MEMORY_ID`; there is no in-memory fallback.
