# Architecture Deep Dive

CEI Life Sciences Bot — an AI-powered life sciences assistant for many isolated users.

Status: this document describes the **target** architecture. Every component below is marked **Exists** or **Planned — not built**. Full detail and rationale live in `.agents/specs/architecture.md` (agent-facing spec); this page links to it instead of repeating it. Source of truth for all decisions: `.agents/decision-log.md` (decisions B1–B86).

---

## Architecture Diagram

![Architecture Diagram](./media/architecture.png)

**This diagram is not updated.** It shows an earlier design (for example, a Fargate cluster for background jobs and separate streaming/REST API Gateway boxes). See "Differences from the diagram" below for what changed. The `.agents/specs/architecture.md` mermaid diagram reflects the current target; this PNG does not.

---

## Status Legend

- **Exists** — deployed, or real code that runs today (locally or via CDK).
- **Exists (partial)** — some code exists, but the AWS resource or production wiring is missing.
- **Planned — not built** — no code or AWS resource exists yet.

---

## 1. Client Access

**Planned — not built.** CloudFront + WAF will front a static SPA build on S3. Amazon Cognito (self sign-up, email verification, no MFA) will authenticate users. Every user's data (chats, keys, attachments, settings) is fully isolated by Cognito `sub` — no sharing, no teams.

The production frontend target is the `founder-ai-dashboard` Vite SPA (React 19, Tailwind 4, react-router). The CEI UI renderer moves into it. **Exists today:** a Next.js 16 test harness in `frontend/` with a local CEI UI preview renderer; this is not the production frontend.

---

## 2. Real-Time Chat (Streaming Path — SSE)

**Planned — not built**, except where noted.

```
Client → CloudFront + WAF → Cognito authorizer → REST API Gateway "/chat/stream" (STREAM)
  → Session Runner Lambda (Node.js, native streaming)
    → Session Router Lambda (deterministic lookup for a dashboard task, OR a small fixed-model
       classifier call for free text; on low classifier confidence, the Router asks the user
       to pick one of the 4 agents with a ChoicePicker instead of guessing)
      → AgentCore Runtime (Research | Drafting | Analysis | Pipeline Manager)
        → AgentCore Gateway (Cedar Policy check) → data source Lambda(s) (including NCBI — specialists
           get every source scope) / Life Sciences Ops / Knowledge Base retrieval / Web Search connector
           (a Gateway target, Cedar-gated)
        → Code Interpreter (not a Gateway target — reached directly, gated by IAM/runtime binding)
        → AgentCoreMemorySaver checkpoint per turn
  ← Session Runner relays chunks as SSE (heartbeat every 15–20 s)
Client renders progressive CEI UI 1.2 patches, ending with the completed surface.
If the plan step's own estimate exceeds 10 minutes, that decision is made once, before any part
of the answer streams, and the run hands off to a background job instead — there is no mid-run
handoff from a streaming run to an async job.
```

A single-source "tool agent" chat (for example, "Ask PubMed") uses its own streaming route, `POST /tools/{source}/chat/stream`, handled by the same Session Runner and the same event catalog; the Router reads `{source}` from the path to pick the profile and the single-source token.

There is **one Regional REST API Gateway (v1)** for every route, not an HTTP API (v2), because v2 cannot stream. `/chat/stream` uses `responseTransferMode: STREAM`; every other route is `BUFFERED`. One Cognito authorizer covers both modes. Limits: 15-minute streaming cap, 5-minute idle, 2 MB/s after the first 10 MB.

**Exists (partial):** the Research and Drafting LangGraph graphs run locally via the AgentCore CLI and return one completed CEI UI 1.1 JSON surface (no streaming). An SSE helper (`shared/transport/sse.py`) can serialize bounded, redacted events, but no production route uses it yet. The Analysis and Pipeline Manager graphs are wired but every node raises `NotImplementedError`.

**Planned:** the Session Runner Lambda, the Session Router Lambda, the REST API Gateway, the Cognito authorizer, and CEI UI 1.2 progressive streaming (see `docs/cei-ui-protocol.md` and `.agents/specs/streaming-protocol.md`).

---

## 3. Background Jobs (Async Path)

**Planned — not built.** Runs predicted to exceed 10 minutes go async, leaving a margin under the 15-minute API Gateway streaming cap. The Pipeline Manager always runs async; the other three agents go async when a plan/triage-step estimate crosses 10 minutes. This decision is made once, from the plan step's own estimate, before any part of the answer streams — there is no mid-run handoff from a streaming run to a background job.

```
Client → REST API Gateway (BUFFERED)
  → Lambda starts an AgentCore Runtime ASYNC session (max 8 h, 2 vCPU / 8 GB)
    → entrypoint returns at once; work runs via add_async_task/complete_async_task;
       runtime pings HealthyBusy
    → AgentCoreMemorySaver checkpoints for crash resume
  → Job ID Store (DynamoDB) tracks status
Client polls GET /jobs/:id or opens /pipeline for live step status.
A watchdog marks stale jobs failed and can resume them from the last checkpoint.
Cancel: a cooperative in-graph flag + StopRuntimeSession.

Pipeline Manager only: each plan step calls the target specialist agent's runtime directly
(InvokeAgentRuntime), not in-process. Concurrent steps run in separate AgentCore Runtime sessions,
each with its own 2 vCPU / 8 GB, and each step gets its own trace.
```

**Fargate is removed from the architecture.** The earlier design used a Fargate task running a copy of the agent; the target design uses AgentCore Runtime async sessions instead. The Job ID Store and a Polling Lambda stay from the earlier design; a watchdog is new.

---

## 4. File Upload

**Planned — not built.** The Attachment Upload Worker Lambda issues a pre-signed S3 POST (max 55 MB, `content-length-range` condition). The file goes to the Attachments Bucket. An S3 event → SQS → Attachment Processor Lambda extracts metadata, checks that the file belongs to the requesting user's chat, writes the Attachment Metadata Store, and triggers Knowledge Base ingestion. Attachments are per-chat only (not shared across chats). Allowed types: PDF, DOCX, TXT, MD, CSV, PNG, JPG. CSV also goes to the Code Interpreter as a file; PNG/JPG also go directly to vision-capable models.

---

## 5. Session and User Management (REST Routes)

**Planned — not built.** All served through the same REST API Gateway:

- **Session Metadata Handler** — create/list/get/update/delete session metadata (DynamoDB).
- **Session History Handler** — cursor-paginated turn history (default page 20, max 100); the trace for a turn loads only when the user expands that turn's thinking block.
- **Fetch User Settings** — user preferences, configured sources, model/mode defaults (DynamoDB).
- **Delete chat / delete account** — return `202 Accepted` at once; a worker removes DynamoDB items, S3 files, Knowledge Base chunks, memories, and checkpoints; the UI hides the item immediately.

Full route table: `docs/APIDoc.md`, detail: `.agents/specs/api.md`.

---

## 6. Data Sources, Tool Agents, and the Gateway

**Planned — not built**, except where noted.

- **10 data source Lambdas** behind AgentCore Gateway (MCP): PubMed, ClinicalTrials.gov, bioRxiv, medRxiv, ChEMBL, Open Targets, CMS Coverage, ICD-10, NPI Registry, PubChem. **Exists (partial):** direct HTTP calls to these sources are implemented in `backend/agents/shared/tools/data_sources/`; there is no Lambda deployment and no Gateway target yet.
- **Tool agents** — 11 single-source natural-language agents (the 10 sources above, plus NCBI: taxonomy, sequence search, BLAST) on **one** AgentCore Runtime, one generic LangGraph graph, and a YAML profile per source. A user chatting with one data source gets a Gateway token scoped to only that source, over its own streaming route, `/tools/{source}/chat/stream` (same Session Runner as `/chat/stream`). Specialist agents never call tool agents; both paths share the same Gateway Lambdas.
- **AgentCore Gateway + Policy (Cedar)** — default-deny. One Cedar permit per source target. `tools/list` is filtered so a caller only sees the tools its scopes allow. Starts in `LOG_ONLY`, then moves to `ENFORCE`.
- **Cognito scopes and tokens** — one resource server (`cei-tools`), one scope per source (for example `cei-tools/src.pubmed`), plus one shared scope for common tools (Life Sciences Ops, Code Interpreter, Web Search, Knowledge Base retrieval) granted to every agent. Specialist agents get every source scope — all 10 sources plus NCBI, 11 total; NCBI is not tool-agent only. Tool agents get exactly one. Only the Session Router holds the Cognito M2M client secret; it requests a token scoped to only what the current request needs, and the AgentCore runtime role cannot read that secret. A pre-token-generation trigger stamps the user's `sub` and session ID onto the token so a Gateway Lambda can read the calling user's identity directly. Before a token expires mid-run, the agent calls an internal token-refresh endpoint (IAM-authenticated) that checks the run is still active and re-issues a token with the same scopes.
- **Life Sciences Operations** — a deterministic Biopython/RDKit Lambda (no model call): sequence validation/transcription/translation/ORF finding, protein and primer properties, chemistry (SMILES/InChI, Lipinski Rule of 5, Tanimoto similarity), and lab calculations (molarity, dilution, unit conversion, HGVS check). Likely a container image because of RDKit's size.
- **Web Search** — only the built-in AgentCore Web Search connector (region-gated to us-east-1, eu-west-1, ap-northeast-1). It is registered as a Gateway target and gated by a Cedar permit under the shared `cei-tools/common` scope, the same enforcement pattern as the other cross-cutting tools — not a bypass of the Gateway. Grounding uses returned snippets only, never full page fetches, and the trace stores only cited snippets (the connector's terms forbid bulk storage).
- **Code Interpreter** — **Exists.** `CodeInterpreterCustom` is defined in `backend/lib/backend-stack.ts` with public network access. By design, it is never a Gateway target (unlike Web Search); it is reached directly and gated by IAM/runtime binding, not by a Cedar permit. It is not yet granted to any runtime.

---

## 7. Agents

Four specialist agents plus the tool-agent runtime, all Python LangGraph applications on Bedrock AgentCore Runtime. Full node-by-node detail: `.agents/specs/agents.md`.

| Agent | Goal | Status |
|---|---|---|
| Research | 1 — deep research, citation grounding | **Exists (partial)** — graph runs locally; no Gateway, no `AgentCoreMemorySaver`, no async, no HITL wired in |
| Drafting | 2 — document preparation and export | **Exists (partial)** — same gaps as Research; also calls the Research graph in-process for its research subgraph (kept in the same runtime/session, not through the Router) |
| Analysis | 3 — competitor/market/legal analysis | **Exists (partial)** — graph wired, every node raises `NotImplementedError` |
| Pipeline Manager | 4 — orchestrates the other agents | **Exists (partial)** — graph wired, every node raises `NotImplementedError`; always runs async; each step is planned to call the target specialist agent's runtime directly (`InvokeAgentRuntime`), in its own session and trace |
| Tool Agent (11 profiles) | single-source chat | **Planned — not built** |

Each specialist agent has science-mode and business-mode system prompt variants, set per chat and changeable mid-chat. Users select their preferred Bedrock model per chat, subject to a configurable global blocklist — no per-user model restriction.

**HITL points (planned, B72):**
- Research — a clarifying question when triage finds no suitable source, or the request is ambiguous.
- Drafting — outline approval before the full draft; revision requests handled through an `interrupt` that resumes into the revise loop.
- Analysis — scope confirmation (competitors, market, jurisdiction) before analysis starts.
- All agents can ask multiple-choice or free-text questions using the existing inline tools (`ask_user_question`, `present_alternatives`, `request_plan_approval`, `get_user_input`).

**Checkpointer (planned):** `AgentCoreMemorySaver` for every agent, replacing today's `InMemorySaver`. It backs crash resume, HITL pause/resume (via a LangGraph `interrupt`, with no fixed time limit on the wait, up to the checkpoint's maximum event expiry — expected around 365 days, exact value to be verified; after that the question shows "expired, start again" instead of resuming), and async-job resume after a kill.

---

## 8. Memory and Knowledge Base

**Planned — not built.**

- **AgentCore Memory (short-term)** — `AgentCoreMemorySaver` checkpointer, described above.
- **AgentCore Memory (long-term)** — built-in strategies only (user preferences, semantic facts, and corrections the user explicitly asks the agent to remember); no explicit "remember" tool, and users cannot see or edit memories in the first release.
- **Knowledge Base** — Amazon Bedrock Knowledge Base backed by **S3 Vectors**. Every attached document is always chunked and indexed, never inserted into context in full. Every chunk carries `user_id`, `session_id`, and `file_id` metadata; every retrieval applies that filter, enforced inside the Gateway Lambda (the agent cannot change the filter). A foundation-model parser also indexes PNG/JPG and CSV; images also go directly to vision-capable models, and CSV also goes to the Code Interpreter.

---

## 9. Guardrails, Prompt Caching, and Model Selection

**Planned — not built**, except prompt caching design (kept from an earlier decision, not yet implemented).

- **Guardrails** — Amazon Bedrock Guardrails on every agent model call. The PII/PHI filter masks user input only; tool results and model output are not filtered, so public data (for example NPI provider names) is not broken by the filter. Guardrail configuration is never shown in the trace.
- **Model selection** — the user selects any Bedrock model per chat; a configurable environment-variable blocklist removes specific model IDs app-wide. **Exists (partial):** `shared/config.py` has one fixed `default_model_id`; there is no picker and no blocklist enforcement yet.
- **Prompt caching** — cache checkpoints on the static system prompt and tool definitions; conversation history is never cached. **Planned — not built:** `get_model()` in `shared/utils.py` sets no `cachePoint` today.

---

## 10. CEI UI Protocol

CEI UI 1.1 is **implemented** for completed JSON responses: content type `application/vnd.cei.ui+json`, protocol `cei-ui`, servers emit version `1.1`, clients and stored history also accept `1.0`. A completed response contains one flat, allowlisted component graph built by deterministic presenters from a typed `ResponseDocument`. Models never emit component records or arbitrary UI; the only model-authored code is the sandboxed `Artifact` component, which runs only inside an iframe sandbox, never in the main page.

**Planned — not built:** CEI UI 1.2 adds progressive SSE patch events (`surface_begin`, `component_add`, `text_delta`, `component_update`, `grounding_update`, `trace_event`, `progress`, `usage`, `hitl_request`, `job_handoff`, `surface_end`, `error`) on top of the same 1.1 component contract — a transport change only, not a new component catalog. This is also how the collapsible "thinking block" is populated: the client renders incoming `trace_event`s while the run is in progress, and collapses the block once the surface completes; the user can re-expand it later from the stored, lazily-loaded trace.

**Exists (partial):** the local Next.js proxy routes can submit and validate typed actions, gated behind `CEI_ENABLE_LOCAL_PROXY=true`; this is a preview only, not the production action endpoint (`POST /ui-actions`, planned).

See [CEI UI Protocol 1.1](./cei-ui-protocol.md) for the full contract, and its "Planned: CEI UI 1.2" section for the streaming addition. Full event catalog: `.agents/specs/streaming-protocol.md`.

---

## 11. Observability, Alarms, and Security

**Planned — not built.**

- **Observability** — AWS-side only: AgentCore Observability (CloudWatch GenAI Observability, OTel traces) and CloudWatch dashboards, visible only to AWS account operators, not connected to the product UI.
- **Alarms** — CloudWatch alarms to an SNS topic with an admin email set by `deploy.sh`, covering: API/Lambda errors (API Gateway 5xx rate, Lambda errors/throttles, DynamoDB throttling), security events (WAF block spikes, Cedar Policy denies, sign-up spikes), and Guardrail intervention spikes. No agent-health alarms — stale jobs are handled by the watchdog and shown in the dashboard instead.
- **Encryption** — AWS-managed keys for DynamoDB, S3, and Secrets Manager; no customer-managed KMS key.
- **Trace content** — the trace shows, in time order: graph steps, tool calls with full inputs/outputs, sources accessed, grounding decisions, and model reasoning text (labeled "summarized" when the provider itself summarized it). It never shows model IDs, guardrail/system prompts, prompt versions, or secrets. The trace is stored forever (until the user deletes the chat) in the Chat History Store; entries over the DynamoDB item limit are gzip-compressed and split across items.

---

## 12. Data Stores

**Planned — not built.** DynamoDB: Session Metadata Store, Chat History Store (trace stored inline), Job ID Store, User Metadata Table, Attachment Metadata Store. S3: Attachments Bucket, S3 Vectors for the Knowledge Base. Secrets Manager: API Key Store, one secret per user per source (`cei/user-keys/{user_id}/{source_id}`). See `docs/provisioned-resources.md` for what is really deployed today (only the Code Interpreter), and `.agents/specs/data-model.md` for full schemas.

---

## 13. Region and Deployment

**Planned — not built.** Region is configurable through environment variables and CDK context — no fixed Region. `deploy.sh` checks that the chosen Region has every required service (AgentCore Runtime/Gateway/Memory/Policy, S3 Vectors, Bedrock Knowledge Base, selected models) before deploying. Single environment; developers use local AgentCore CLI runs. All AgentCore resources (runtimes, Gateway and targets, Memory, Policy engine, Knowledge Base) go into CDK; `agentcore.json` stays local-development-only. See `docs/deploymentGuide.md`.

---

## Differences from `docs/media/architecture.png`

The diagram at `docs/media/architecture.png` is **not updated** and does not reflect the decisions below. Treat this table, not the picture, as current.

| Original diagram | Target (this document) | Why |
|---|---|---|
| Fargate Agent Runner Cluster for background jobs | AgentCore Runtime **async sessions** (max 8 h, 2 vCPU/8 GB) | Fargate is removed; a watchdog is added for stale-job recovery |
| Separate "Streaming API Gateway" and "REST API Gateway" boxes | **One** Regional REST API (v1) for all routes | HTTP API v2 cannot stream; one API with a per-route transfer mode |
| Session Router shown as a simple no-model dispatcher | Session Router does deterministic lookup **and** a small fixed-model classifier call for free text | Routing is unchanged for dashboard tasks; free-text chat is new |
| No tool-agent runtime | New AgentCore Runtime: 1 generic graph, 11 YAML profiles (10 sources + NCBI) | Single-source natural-language chat, separate from the specialist agents |
| Generic "Web search" box | The specific AgentCore built-in `web-search` connector, region-gated | Connector terms forbid bulk storage; only cited snippets are kept |
| No policy/authorization layer in front of the Gateway | Policy in AgentCore (Cedar), default-deny, `tools/list` filtering | Needed for per-user, per-source tool scoping |
| "AgentCore Managed RAG" (unspecified) | Bedrock Knowledge Base backed by **S3 Vectors**, per-chunk metadata filter | Concrete storage and isolation choice |
| No Life Sciences Operations Lambda | New deterministic Biopython/RDKit Lambda | Domain-specific helpers with no model call |
| Static dashboard report pages (Target Assessment, Variant Report, Regulatory Brief) | Removed | Demo-only mock-ups, not part of the target product |

---

## Design Decisions

### Code Execution: AgentCore Code Interpreter

**Exists.** AgentCore provides a built-in Code Interpreter — a containerized sandbox supporting Python, JS, and TS. Each execution runs in an isolated container. Network access is configurable. Timeout: 15 min default, up to 8 hours. File uploads: 100 MB inline, 5 GB via S3. `CodeInterpreterCustom` is defined in `backend/lib/backend-stack.ts` with public network access (needed for calls to external database APIs and for installing Python packages).

### Custom Tool Integration Strategy

| Tool type | Approach | Status |
|---|---|---|
| Life Sciences Operations | AgentCore Gateway Lambda target | Planned |
| Code sandbox (data analysis, transformations) | AgentCore Code Interpreter | Exists |
| Web search | Built-in AgentCore Web Search connector | Planned |
| Public data source search | AgentCore Gateway Lambda targets (10 sources + NCBI) | Planned (direct HTTP calls exist locally) |

---

## Open Questions

Still open:

1. Design of user-configured paid data sources (deferred).
2. Trace key schema for entries over the DynamoDB item limit (chunk size is decided: 350 KB).
3. AgentCore Memory short-term event expiry: the exact maximum number still needs verification (expected around 365 days); the HITL behavior once it expires ("expired, start again") is already decided.
4. Product name and branding.
5. Router low-confidence threshold value for when to ask the user to pick an agent (an implementation default, tuned later).

Resolved since the decision log's earlier "Still open" list: the async threshold for background-job prediction is 10 minutes, decided once before streaming starts, with no mid-run handoff.

Full architecture spec, request-flow diagrams, and the component-by-component phase table: `.agents/specs/architecture.md`.
