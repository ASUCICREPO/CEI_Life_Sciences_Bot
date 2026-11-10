# Project Modification Guide

This guide is for developers who want to extend or modify the CEI Life Sciences Bot. It reflects real paths in the repository today, and marks planned work clearly.

---

## Project Structure Overview

```
├── backend/
│   ├── bin/backend.ts              # CDK app entry point
│   ├── lib/backend-stack.ts        # BackendStack: Cognito, S3, CloudFront, REST API, tables, Lambdas
│   ├── lib/edge-waf-stack.ts       # EdgeWafStack (us-east-1 WAF)
│   ├── lib/constructs/             # agent-runtimes, tool-gateway, session-router, background-jobs, observability, ...
│   ├── lambdas/                    # One directory per Lambda (session-runner, session-router, ui-actions,
│   │                               #   session-*-handler, exports-handler, jobs-handler, watchdog,
│   │                               #   data-source-<id>, ncbi, lsops, token-refresh, pre-token-generation, ...)
│   ├── agents/                     # Agent code — LangGraph graphs, tools, presenters
│   │   ├── main.py                 # Research runtime entry point
│   │   ├── drafting_main.py        # Drafting runtime entry point
│   │   ├── analysis_main.py        # Analysis runtime entry point
│   │   ├── pipeline_main.py        # Pipeline Manager runtime entry point
│   │   ├── research/               # Research agent graph and nodes
│   │   ├── drafting/                # Drafting agent graph, nodes, export service
│   │   ├── analysis/                # Analysis agent graph (MVP slice, B94)
│   │   ├── pipeline_manager/        # Pipeline Manager graph (MVP slice, B94)
│   │   ├── tool_agent/              # Generic tool-agent graph and YAML profiles (no runtime yet)
│   │   └── shared/                  # Tools, schemas, presenters, prompts, grounding, memory, persistence, streaming
│   │       ├── tools/data_sources/  # Data source clients; the Gateway Lambdas wrap them
│   │       ├── tools/hitl/          # ask_user_question, present_alternatives, request_plan_approval, get_user_input
│   │       ├── presenters/          # Deterministic ResponseDocument → CEI UI presenters
│   │       ├── schemas/             # CEI UI, ResponseDocument, citation schemas
│   │       └── streaming/           # CEI UI 1.2 event stream (Research)
│   └── test/                        # CDK stack tests (Jest), one file per stack or construct
├── frontend/                        # Vite + React 19 SPA (founder-ai-dashboard, B1/B66)
│   ├── src/                         # pages/, components/ (cei-ui/, streaming/), lib/ (API clients, stream reducer)
│   └── public/
├── archive/frontend-next/           # Archived Next.js test harness (reference only, not deployed)
└── docs/                            # Human-facing documentation (this file's directory)
```

Lambda code lives in `backend/lambdas/<name>/` (plural). Agent code lives in `backend/agents/`. Treat any older reference to `backend/agent/` (singular) or `backend/lambda/` as out of date. The production frontend is the Vite SPA in `frontend/`. The old Next.js app is in `archive/frontend-next/` (B1/B66).

---

## Adding a new data source

Each data source is a Gateway Lambda target, gated by a Cedar permit and a Cognito scope. `backend/lib/constructs/tool-gateway.ts` builds the Lambda, the Gateway target, the scope, and the Cedar permit for each entry in `backend/lambdas/data-source-tool-schemas.json`.

1. **Write the source client** in `backend/agents/shared/tools/data_sources/` (for example `pubmed.py`). Reuse the request/response shape and citation structure of an existing client (see D5 in `docs/agent-architecture-decisions.md`).
2. **Write the Lambda handler** in `backend/lambdas/data-source-<id>/handler.py`. Use `make_handler` from `backend/lambdas/shared/data_source_runtime.py`, as `data-source-pubmed/handler.py` does.
3. **Add the tool schema** to `backend/lambdas/data-source-tool-schemas.json` (`sourceId` and `tool`). The construct then makes the Gateway target and the Cedar permit.
4. **Check the new Cognito scope.** The construct adds `cei-tools/src.<id>` to the resource server. Make sure that it is not a substring of any existing scope name and does not contain any existing scope name as a substring (Cedar's `like` operator does substring matching — see `.agents/specs/auth-and-tool-scope.md`).
5. **If the source should also be a single-source tool agent**, add a YAML profile for it in `backend/agents/tool_agent/profiles/` (see "Adding a tool agent" below).
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
4. Add an entry point (`<agent>_main.py`) and add the agent to `AGENT_RUNTIMES` in `backend/lib/constructs/agent-runtimes.ts`. Research shows the current wiring for Gateway tools and `AgentCoreMemorySaver`; the other runtimes still use `InMemorySaver`.
5. Add the agent to the Session Router's deterministic task-to-agent lookup (`backend/lambdas/session-router/handler.py`). The free-text classifier output set (`.agents/specs/agents.md` §6) is planned, not built. Also add it to the low-confidence `ChoicePicker` option list the Router shows the user when the classifier is unsure (today that list is the fixed set of 4 agents).

### Adding a tool agent (partly built)

Tool agents are a single AgentCore Runtime with one generic graph and one YAML profile per data source (B21/B27/B29). The graph and profiles exist in `backend/agents/tool_agent/`. The runtime and the `/tools/{source}/chat/stream` route are not built (F15).

1. Add a YAML profile describing the source's name, its one Cognito scope, and any source-specific hooks (for example NCBI BLAST's asynchronous submit/poll pattern).
2. The generic graph loads the profile for the requested source at request time and restricts the agent to that source's Gateway scope plus the shared tools scope (Life Sciences Ops, Code Interpreter, Web Search, Knowledge Base — B70).
3. Register the new profile in the tool-agent runtime's profile list; no new runtime deployment is needed per source.
4. Tool-agent chat is reached over its own streaming route, `POST /tools/{source}/chat/stream`, not `/chat/stream` — the Session Router reads `{source}` from the path to pick the profile.

---

## Modifying the CDK stack

**Location:** `backend/lib/backend-stack.ts`

This file defines `BackendStack`. Larger parts live in `backend/lib/constructs/`. Follow the existing pattern for any new construct: add an ADR-format comment (`ADR: <title> | Rationale: <why> | Alternative: <rejected>`) above any non-obvious choice, and add cdk-nag suppressions with the same ADR format when a finding is intentionally accepted. See `CLAUDE.md` "Key Conventions".

---

## Changing AI/ML models

**Location:** `backend/agents/shared/config.py` (`default_model_id`), `backend/agents/shared/utils.py` (`get_model()`).

Today there is one deployment default model ID (`CEI_DEFAULT_MODEL_ID`) and no blocklist enforcement. The planned per-chat model picker and the environment-variable blocklist (D3, B18, B76) are not implemented. `backend/agents/shared/runtime/model_policy.py` is a foundation that no call site uses yet.

**Modifying prompts:** science/business system prompt variants load through `backend/agents/shared/prompts/loader.py`. Add or edit prompt files there, keeping the mode split intact.

---

## Database modifications

The DynamoDB tables and S3 buckets that exist are listed in [Provisioned Resources](./provisioned-resources.md). Repository code for the tables is in `backend/agents/shared/persistence/`. To change or add a table, follow `.agents/specs/data-model.md` for schema and key design, and partition every item by Cognito `sub` per B12.

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
# Frontend (Vite dev server)
cd frontend
npm run dev

# Backend (synthesize CDK)
cd backend
npm run build
npx cdk synth

# Agents (run locally; agentcore dev uses backend/agents/agentcore/)
cd backend/agents
uv run python main.py
```

### Deployment testing

```bash
cd backend
npx cdk deploy --hotswap  # for faster Lambda updates in a development account
```

---

## Conclusion

This project is designed to be extensible. Some extension points above are still planned or partly built. Check `docs/architectureDeepDive.md` and `.agents/decision-log.md` for the current status of any component before building against it.
