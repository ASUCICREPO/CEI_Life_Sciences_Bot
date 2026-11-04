# AGENTS.md

This file provides guidance to Codex (claude.ai/code) when working with code in this repository.

## Project Overview

CEI Life Sciences Bot — an AI-powered life sciences assistant for many isolated users. It has a dashboard frontend and an AWS CDK backend, and uses Amazon Bedrock AgentCore for agent orchestration with domain-specific tools. The production frontend is now the Vite dashboard in `frontend/`; the prior Next.js test harness moved to `archive/frontend-next/` (B1/B66, built in F2).

## Architecture

Monorepo with separate `package.json` per directory (no workspace root).

- **frontend/** — the `founder-ai-dashboard` Vite SPA (React 19, Tailwind CSS 4, react-router), served from S3 + CloudFront + WAF and gated by Cognito login (F2, B1/B66/B88–B90). The archived Next.js 16 test harness (its CEI UI preview renderer, zod schema, and local proxy routes) lives in `archive/frontend-next/` for reference.
- **backend/** — AWS CDK (TypeScript). Two stacks: `BackendStack` in `lib/backend-stack.ts` (Code Interpreter, Cognito, the pre-sign-up domain check, and the S3/CloudFront/WAF-fronted dashboard) and `EdgeWafStack` in `lib/edge-waf-stack.ts` (the `CLOUDFRONT`-scope WAF web ACL, always `us-east-1`, B88), joined by `crossRegionReferences: true`. Entry point `bin/backend.ts`. Uses `cdk-nag` AwsSolutionsChecks scoped to each stack (not the app). `backend/agents/` holds the real LangGraph agent code (Research, Drafting, Analysis, Pipeline Manager) that runs locally today via the AgentCore CLI.

### Current Implementation

- Research and Drafting AgentCore runtime entry points run locally (AgentCore CLI) and return completed JSON responses. Analysis and Pipeline Manager graphs are wired but every node raises `NotImplementedError`.
- Completed rich responses use CEI UI 1.1 in the `ui` field (servers emit 1.1; clients and stored history also accept 1.0). Backend and frontend validators enforce the allowlisted component graph and bounded typed actions.
- The archived Next.js app (`archive/frontend-next/`) still has local agent and action proxy routes plus a CEI UI preview renderer; the proxy routes require `CEI_ENABLE_LOCAL_PROXY=true` and are not used by the current or planned production deployment.
- An SSE helper serializes bounded, redacted events, but no production streaming route uses it.
- The CDK stacks define the AgentCore Code Interpreter, a Cognito user pool + SPA client (managed login, PKCE) with a pre-sign-up email-domain allowlist trigger, and an S3 + CloudFront + WAF static site serving the built `frontend/` SPA. See `docs/provisioned-resources.md` for exactly what is deployed today.
- There is still no REST API Gateway, no Session Runner, no Session Router, no AgentCore Gateway, and no actual LangGraph pause-and-resume flow yet.

### Planned Architecture (target — not built; see `docs/architectureDeepDive.md`)

- **One Regional REST API Gateway (v1)** for every route. `/chat/stream` uses `responseTransferMode: STREAM`; every other route is `BUFFERED`. One Cognito User Pools authorizer. (HTTP API v2 cannot stream, so REST API v1 replaces it.)
- **Streaming path (SSE)** — Client → CloudFront (WAF) → Cognito authorizer → REST API `/chat/stream` → Session Runner Lambda (Node.js, native streaming) → Session Router Lambda (Python: deterministic lookup for dashboard tasks, or a small fixed-model classifier for free text) → AgentCore Runtime (Research | Drafting | Analysis | Pipeline Manager).
- **Async path (background jobs)** — AgentCore Runtime **async sessions** (max 8 h, 2 vCPU/8 GB) replace Fargate. A watchdog resumes stale jobs from checkpoint. The Job ID Store (DynamoDB) and a Polling Lambda track and report status; `/pipeline` shows live step status.
- **Many isolated users** (not single-user): self-sign-up Cognito with email verification, no MFA, every record partitioned by Cognito `sub`.
- **Tool agents**: 11 single-source natural-language tool agents (10 data sources + NCBI) run on one AgentCore Runtime with one generic graph and a YAML profile per source. Specialist agents call the Gateway directly and never go through a tool agent.
- **AgentCore Gateway + Policy (Cedar)**: default-deny, one Cedar permit per source target, `tools/list` filtered by scope. Cognito issues short-lived, scope-limited M2M tokens (one scope per source; specialist agents get all scopes, tool agents get exactly one) held only by the Session Router.
- **Web Search**: the built-in AgentCore Web Search connector only (region-gated), not a general web search tool.
- **Life Sciences Operations**: a deterministic Biopython/RDKit Lambda behind the Gateway (sequence tools, protein/primer properties, chemistry, lab calculations) — no model call.
- **Guardrails**: Amazon Bedrock Guardrails on every agent model call; the PII/PHI filter masks user input only.
- **CEI UI 1.2**: adds progressive SSE streaming (patch events) on top of the existing 1.1 component contract; see `docs/cei-ui-protocol.md`.
- **Frontend**: the Vite dashboard is the production UI (`frontend/`, F2 built); a trace/thinking block (planned) renders the streamed reasoning and tool-call log.

### Planned AWS Services

| Service | Role |
|---|---|
| **CloudFront + WAF + S3** | Static dashboard hosting behind a CDN and WAF |
| **Cognito** | Self-sign-up user authentication, SPA PKCE client, Router M2M client |
| **REST API Gateway (v1, Regional)** | Single API; STREAM on `/chat/stream`, BUFFERED elsewhere |
| **Lambda** | Session Runner (Node.js), Session Router, Polling, Session Metadata/History Handlers, Fetch User Settings, Attachment Upload Worker, Attachment Processor |
| **AgentCore Runtime** | Research, Drafting, Analysis, Pipeline Manager, and the Tool Agent runtime — async sessions replace Fargate |
| **AgentCore Gateway + Policy (Cedar)** | MCP tool access with per-source scope enforcement |
| **Bedrock AgentCore** | Agent runtime, Memory (short- and long-term), Knowledge Base, Web Search connector |
| **SQS** | Attachment processing queue |
| **DynamoDB** | Session Metadata, Chat History, Job ID, User Metadata, Attachment Metadata Stores |
| **S3** | Attachments Bucket (pre-signed upload, max 55 MB), S3 Vectors for the Knowledge Base |
| **Secrets Manager** | API Key Store (user-configured service credentials) |
| **CloudWatch + SNS** | Dashboards, alarms, admin email notifications |

### Planned Agent Topology (Multi-Agent)

| Agent | Goal | Role |
|---|---|---|
| Research Agent | 1 | Deep research across 10 data sources with citation grounding |
| Drafting Agent | 2 | Document preparation (grants, VCs, journals, whitepapers) |
| Analysis Agent | 3 | Competitor/market/legal landscape analysis |
| Pipeline Manager | 4 | Orchestrates other agents in concurrent/sequential/looped tasks; always runs async |
| Tool Agent (11 profiles) | — | Single-source natural-language chat for users who want one data source directly |

The dashboard routes Goals 1–3 through a deterministic Session Router lookup; free-text chat adds a small fixed-model classifier. Goal 4 always uses the Pipeline Manager. Each specialist agent has science-mode and business-mode system prompt variants, set per chat. Users select their preferred Bedrock model per chat, subject to a configurable global blocklist.

### Agent-to-UI Protocol: CEI UI

The approved production protocol is CEI UI 1.1 (`application/vnd.cei.ui+json`, protocol `cei-ui`; servers emit 1.1, clients and stored history also accept 1.0). Completed JSON responses use the `ui` field with a flat allowlisted component graph. Deterministic presenters build the graph from a typed `ResponseDocument` of presentation blocks. Models never emit component records or arbitrary UI; the only model-authored code is sandboxed Artifact HTML/JS, confined to an iframe sandbox and never executed in the main page. Typed POST dispatch and validation exist in the local preview; the production action endpoint is planned. CEI UI 1.2 (planned) adds progressive SSE patch events on the same component contract. See `docs/cei-ui-protocol.md`.

### HITL: AgentCore Inline Functions

Current helper tools define `ask_user_question`, `present_alternatives`, `request_plan_approval`, and `get_user_input`. They render CEI UI components, and the local preview can send typed POST actions. The current helpers do not pause or resume LangGraph. Planned: real pause and resume through a LangGraph `interrupt`, checkpointed with `AgentCoreMemorySaver`, resumed by a typed POST action with no time limit on the wait.

### Planned Data Stores

- **Chat History Store** — indefinite retention (until the user deletes it), lazy cursor-paginated loading for large histories.
- **User Metadata Table** — user settings, configured sources, AI provider preferences.
- **API Key Store** — user-owned API keys stored in Secrets Manager per user.
- **Knowledge Base (Bedrock KB + S3 Vectors)** — RAG over chat attachments, filtered by `user_id`/`session_id`/`file_id`.

### Key Design Documents

- `docs/agent-architecture-decisions.md` — historical agent architecture decisions (D1–D17), with supersession notes
- `docs/architectureDeepDive.md` — full target architecture with service mapping and status per component
- `docs/cei-ui-protocol.md` — CEI UI 1.1 surface and action contract, plus the planned 1.2 streaming addition

## Planning files for agents

Active planning work lives outside `docs/` in `.agents/`:

- `.agents/decision-log.md` — the working decision log (B1–B86 and the phase list). **This file wins over every other doc, including this one, on any conflict.**
- `.agents/roadmap.md` — phase sequencing.
- `.agents/specs/` — detailed specs (architecture, data model, API, streaming protocol, auth and tool scope, agents, tools, security) that expand on the decision log.
- `.agents/phases/` — one file per build phase (S1–S5, F1–F21).

`docs/` describes the target state for humans, each item marked "Planned" or "Exists"; `.agents/` is where agents track live decisions and specs. New architecture decisions need user approval before they go in the decision log.

## Build & Test Commands

### Backend (run from `backend/`)
```bash
npm install
npm run build          # tsc
npm test               # jest (all tests)
npx jest test/foo.test.ts  # single test
npx cdk synth          # synthesize CloudFormation
npx cdk deploy         # deploy stack
```

### Frontend (run from `frontend/`)

The Vite dashboard is now the frontend in `frontend/` (F2 replaced the Next.js test harness — see `.agents/decision-log.md` B1/B66; the archived Next.js app's commands, if you need them, are unchanged in `archive/frontend-next/`).

```bash
npm install
npm run dev            # local dev server (Vite)
npm run build          # tsc -b && vite build
npm run lint           # oxlint
```

## Key Conventions

- cdk-nag suppressions require ADR-format reason strings: `ADR: <title> | Rationale: <why> | Alternative: <rejected>`.
- Specialized Codex subagents exist under `.Codex/agents/` for backend, frontend, deployment, documentation, and security tasks.
- Security scanning commands are available via `/cic-security-check`.
- Commit messages: 5 words max, no watermark/attribution line, relevant to the change.
- Commit scope: group related changes only; do not bundle unrelated files into one commit.
