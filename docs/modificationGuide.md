# Project Modification Guide

This guide is for developers who want to extend or modify the CEI Life Sciences Bot. It reflects real paths in the repository today, and marks planned work clearly.

---

## Project Structure Overview

```
├── backend/
│   ├── bin/backend.ts              # CDK app entry point
│   ├── lib/backend-stack.ts        # Infrastructure definitions (Code Interpreter only, today)
│   ├── agents/                     # Real agent code — LangGraph graphs, tools, presenters
│   │   ├── main.py                 # Research agent entry point (runs locally via AgentCore CLI)
│   │   ├── drafting_main.py        # Drafting agent entry point
│   │   ├── research/               # Research agent graph and nodes
│   │   ├── drafting/                # Drafting agent graph and nodes
│   │   ├── analysis/                # Analysis agent graph (nodes raise NotImplementedError)
│   │   ├── pipeline_manager/        # Pipeline Manager graph (nodes raise NotImplementedError)
│   │   └── shared/                  # Tools, schemas, presenters, prompts, grounding, transport
│   │       ├── tools/data_sources/  # Direct HTTP calls to the 10 public data sources (today)
│   │       ├── tools/hitl/          # ask_user_question, present_alternatives, request_plan_approval, get_user_input
│   │       ├── presenters/          # Deterministic ResponseDocument → CEI UI presenters
│   │       ├── schemas/             # CEI UI, ResponseDocument, citation schemas
│   │       └── transport/sse.py     # SSE serialization helper (no production route uses it yet)
│   └── test/
│       └── backend.test.ts          # CDK stack tests
├── frontend/                        # Next.js 16 test harness today (planned: replaced by the Vite dashboard, B1/B66)
│   ├── app/                         # Next.js pages, local proxy routes, CEI UI preview renderer
│   └── public/
└── docs/                            # Human-facing documentation (this file's directory)
```

There is no `backend/lambda/` directory yet — Lambdas (Session Runner, Session Router, data source targets, and the rest) are **planned, not built**. `backend/agents/` is where the real agent code lives; treat any older reference to `backend/agent/` (singular) or `backend/lambda/` as out of date.

**Planned repository change (B1/B66):** the production frontend becomes the `founder-ai-dashboard` Vite SPA. When that happens, today's `frontend/` (Next.js) moves to `archive/frontend-next/` for reference — its CEI UI renderer and zod schema are expected to carry over into the new frontend, not be rewritten from scratch.

---

## Adding a new data source (Planned — not built)

The full target pattern is a Gateway Lambda target, gated by a Cedar permit and a Cognito scope. None of the Gateway, Policy, or Cognito pieces exist yet; this is the plan for when they do.

1. **Write the Lambda handler.** Model it on an existing direct-call implementation in `backend/agents/shared/tools/data_sources/` (for example `pubmed.py`) — reuse its request/response shape and citation structure (see D5 in `docs/agent-architecture-decisions.md`).
2. **Register it as an AgentCore Gateway target** (MCP) in CDK, alongside the other source targets.
3. **Add one Cognito scope** for the new source in the resource server (for example `cei-tools/src.<newsource>`), checking it is not a substring of any existing scope name and does not contain any existing scope name as a substring (Cedar's `like` operator does substring matching — see `.agents/specs/auth-and-tool-scope.md`).
4. **Add one Cedar permit** for the new source's Gateway actions, scoped to the new Cognito scope (one permit per source target, per B69).
5. **If the source should also be a single-source tool agent**, add a YAML profile for it under the tool-agent runtime's profile set (see "Adding an agent" below) — this makes it the 12th tool-agent profile.
6. **Grant the new scope** to every specialist agent (they get all source scopes, including NCBI's — no source is specialist-agent-exempt) and to the new tool-agent profile only (exactly one scope).
7. Update `.agents/specs/tools.md` and this guide's data source list.

**Note on the Web Search connector and Code Interpreter:** if the new item is a built-in AgentCore connector (like Web Search) rather than a custom Lambda, it is still registered as a Gateway target and still needs a Cedar permit — normally under the shared `cei-tools/common` scope, not a new per-source scope. Code Interpreter is the one exception: it is never a Gateway target, so it is never gated by a Cedar permit, only by IAM/runtime binding.

---

## Adding an agent

### Adding a new specialist agent

The four specialist agents (Research, Drafting, Analysis, Pipeline Manager) each follow the same LangGraph pattern under `backend/agents/<agent_name>/` — real, working code for Research and Drafting today; Analysis and Pipeline Manager are wired but every node raises `NotImplementedError`.

1. Create `backend/agents/<agent_name>/{state.py,graph.py,nodes.py,__init__.py}` following the existing structure (see `backend/agents/research/` for a complete example).
2. Define the agent's `ResponseDocument`-producing node(s) using `backend/agents/shared/schemas/response_blocks.py` and `shared/grounding.py`, so output reaches CEI UI through the existing deterministic presenter path (`shared/presenters/cei_ui.py`).
3. Add a mode-aware system prompt pair (science/business) via `shared/prompts/loader.py`, matching D4/B55.
4. **Planned, not yet available to wire in:** `AgentCoreMemorySaver` checkpointing, Gateway-scoped tool access, async execution, and Session Router dispatch. Until those exist, new agents run the same way Research and Drafting do today — locally, via the AgentCore CLI, with `InMemorySaver`.
5. Add the agent to the Session Router's deterministic task-to-agent lookup and, if it should be reachable from free-text chat, to the router classifier's output set (`.agents/specs/agents.md` §6) — planned, not built. Also add it to the low-confidence `ChoicePicker` option list the Router shows the user when the classifier is unsure (today that list is the fixed set of 4 agents).

### Adding a tool agent (Planned — not built)

Tool agents are a single AgentCore Runtime with one generic graph and one YAML profile per data source (B21/B27/B29) — none of this exists in the repository yet.

1. Add a YAML profile describing the source's name, its one Cognito scope, and any source-specific hooks (for example NCBI BLAST's asynchronous submit/poll pattern).
2. The generic graph loads the profile for the requested source at request time and restricts the agent to that source's Gateway scope plus the shared tools scope (Life Sciences Ops, Code Interpreter, Web Search, Knowledge Base — B70).
3. Register the new profile in the tool-agent runtime's profile list; no new runtime deployment is needed per source.
4. Tool-agent chat is reached over its own streaming route, `POST /tools/{source}/chat/stream`, not `/chat/stream` — the Session Router reads `{source}` from the path to pick the profile.

---

## Modifying the CDK stack

**Location:** `backend/lib/backend-stack.ts`

Today this file defines only the `CodeInterpreterCustom` construct and its stack output. Follow the existing pattern for any new construct: add an ADR-format comment (`ADR: <title> | Rationale: <why> | Alternative: <rejected>`) above any non-obvious choice, and add cdk-nag suppressions with the same ADR format when a finding is intentionally accepted. See `CLAUDE.md` "Key Conventions".

---

## Changing AI/ML models

**Location:** `backend/agents/shared/config.py` (`default_model_id`), `backend/agents/shared/utils.py` (`get_model()`).

Today there is one fixed model ID and no blocklist enforcement. The planned per-chat model picker and the environment-variable blocklist (D3, B18, B76) are not implemented — `get_model()` already accepts a `model_id` override parameter, but no call site passes a user-selected value yet.

**Modifying prompts:** science/business system prompt variants load through `backend/agents/shared/prompts/loader.py`. Add or edit prompt files there, keeping the mode split intact.

---

## Database modifications (Planned — not built)

No DynamoDB tables, S3 buckets (other than a future Attachments/static-hosting bucket), or Secrets Manager secrets exist yet. When they are added, follow `.agents/specs/data-model.md` for schema and key design, and partition every item by Cognito `sub` per B12.

---

## Best practices

1. **Test locally before deploying** — use `cdk synth` to validate CDK changes; use the AgentCore CLI to run agent graphs locally.
2. **Use environment variables** — do not hardcode Region, model IDs, or secrets.
3. **Follow existing patterns** — match the structure already used in `backend/agents/research/` and `shared/` when adding new agents or tools.
4. **Update documentation** — keep this guide, `docs/architectureDeepDive.md`, and `.agents/specs/` in sync with code changes.
5. **Version control** — make small, focused commits.

---

## Testing your changes

### Local testing

```bash
# Frontend (today's Next.js test harness)
cd frontend
npm run dev

# Backend (synthesize CDK)
cd backend
npm run build
npx cdk synth

# Agents (run locally via AgentCore CLI, see backend/agents/test_commands.md)
cd backend/agents
uv run python main.py
```

### Deployment testing

```bash
cd backend
npx cdk deploy --hotswap  # for faster Lambda updates, once Lambdas exist
```

---

## Conclusion

This project is designed to be extensible. Most of the extension points above are currently planned, not built — check `docs/architectureDeepDive.md` and `.agents/decision-log.md` for the current status of any component before building against it.
