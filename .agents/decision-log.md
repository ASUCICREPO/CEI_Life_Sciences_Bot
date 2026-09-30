# Build Plan — Working Decision Log

Status: DRAFT. This file records decisions while we plan. When planning is complete, the other `docs/` files are updated from it.

Sources: `docs/media/architecture.png`, current code in `backend/` and `frontend/`, and the `founder-ai-dashboard` prototype.

## Decisions

| # | Area | Decision | Notes |
|---|---|---|---|
| B1 | Frontend | The production frontend is the `founder-ai-dashboard` shell (Vite SPA, React 19, Tailwind 4, react-router `BrowserRouter`). The CEI UI renderer moves into it and is styled with the dashboard design system. The Next.js `frontend/` is archived. | The static build goes to S3. CloudFront needs an SPA fallback (403/404 → `index.html`). The NCBI calls use the Vite dev proxy, so they need a backend path in production. |
| B2 | API | One Regional REST API (API Gateway v1) for **all** routes. `/chat/stream` uses `responseTransferMode: STREAM`. The other routes use BUFFERED. One Cognito User Pools authorizer. | HTTP API (v2) cannot stream. The edge-optimized endpoint has a 30 s idle timeout, so use a Regional endpoint behind our own CloudFront. Streaming limits: 15 min maximum, 5 min idle, 2 MB/s after the first 10 MB. Test the Cognito authorizer on the streaming method early. |
| B3 | Lambda language | The Session Runner is Node.js/TypeScript (native response streaming). All other Lambdas are Python. | Agents stay in Python (LangGraph) on AgentCore Runtime. |
| B4 | Streaming unit | Progressive SSE, component by component, in order. The UI builds while the agent works. The UI must not appear all at once at the end. | The CEI UI protocol needs a patch/append event set (a new protocol version). The browser uses `fetch` + ReadableStream, because `EventSource` cannot send `Authorization`. The server sends an SSE heartbeat every 15–20 s. CloudFront uses CachingDisabled and no compression on `/chat/*`. |
| B5 | Progress UI | While the agent works, the user sees: agent step status, tool calls and sources, and a live token/cost counter. | |
| B6 | Thinking block | A collapsible thinking block, like ChatGPT and Claude. It is expanded while the agent works and collapses when the final answer is ready. The user can scroll up and expand it again. | |
| B7 | Trace content | The trace shows, in time order: graph steps, tool calls with full inputs and outputs, sources accessed, grounding decisions, and the model reasoning text when the provider returns it (labeled "summarized" when the provider summarized it). | **Never shown:** model IDs, guardrail/system prompts, prompt versions, API keys or other secrets. |
| B8 | Audit storage | The trace is stored forever in the raw Chat History DynamoDB table. There is no separate S3 audit store. Secrets are removed before storage. | Aligns with D12 (indefinite retention). |
| B9 | Large trace entries | Trace payloads larger than the DynamoDB item limit (400 KB) are gzip-compressed and split into multiple DynamoDB items. No S3 overflow. | Reads must reassemble the chunks. Details (chunk size, key schema) are TBD in the data model section. |
| B10 | Streamed grounding | Text components stream token by token with a "verifying citations" pending state. When grounding for the block completes, citation chips attach and claims without citations get a flag. | Grounding stays server-side (D5/D7). |
| B11 | Scope | Plan the full architecture diagram, in phases. | Phase boundaries are TBD. |
| B12 | Users | Many users, fully isolated. Each user sees only their own chats, keys, attachments and settings. No sharing, no teams. | Replaces the "single-user system" wording of D15. All data is partitioned by Cognito `sub`. |
| B13 | Sign-up | Cognito self sign-up with email verification. | |
| B14 | MFA | No MFA. | |
| B16 | Usage limits | MVP limits: email domain allowlist on sign-up (Cognito pre-sign-up trigger), API Gateway throttling, and a limit on concurrent runs for each user. No token or cost budget for the MVP. | Values TBD. |
| B17 | Router classifier | A separate Python Session Router Lambda. Task-based requests use a deterministic lookup. Free text goes to a small, fast, fixed Bedrock model with structured output `{agent, confidence}`. | The classifier model does not depend on the user's model selection. |
| B18 | Model selection | Keep D3: the user selects any Bedrock model per chat. The blocklist is configurable through an environment variable. | |
| B19 | Data source access | Keep D2: each data source is a Python Lambda target behind AgentCore Gateway (MCP). AgentCore agents and the Fargate agent use the same Gateway. Keys are read from Secrets Manager inside the Lambda. | Remove `CEI_DIRECT_MODE` from production paths. |
| B20 | User API keys | All current sources work without a key. A user can add an optional key (for example NCBI) to get higher rate limits. Users can also configure additional paid sources that need API keys. | The design of paid sources is deferred to a later discussion. |
| B21 | Tool agents | Each data source gets its own "tool agent" on a separate AgentCore runtime. A tool agent is a natural-language layer over one data source API. It gives a guided interface to query that API and analyse the results. Its only external data tool is its own source. Standard tools (for example the Code Interpreter) stay available. NCBI (taxonomy, sequence search, BLAST) in the dashboard is one example of this pattern. | Remove the direct browser calls and the Vite dev proxy. Open: the exact source list, one runtime per source or shared code, and how the specialist agents use tool agents. |
| B26 | Specialist data path | Specialist agents (Research, Drafting, Analysis, Pipeline Manager) call the data source Lambdas directly through AgentCore Gateway. They do not call tool agents. Tool agents are for users who chat with one data source. Both paths use the same Gateway Lambdas. | |
| B27 | Tool agent list | 11 tool agents: PubMed, ClinicalTrials.gov, bioRxiv, medRxiv, ChEMBL, Open Targets, CMS Coverage, ICD-10, NPI, PubChem, and NCBI (taxonomy, sequence search, BLAST). | NCBI adds new Gateway Lambda targets (BLAST, taxonomy, sequence). |
| B23 | Async trigger | Runs that are predicted to take more than 15 minutes run as background jobs. Other runs are real-time over SSE. Prediction: Pipeline Manager runs are always async. For the other agents, the plan step estimates the steps and sources, and the run goes async if the estimate is more than a threshold. | Threshold value TBD. |
| B24 | Job notification | Keep D14: the dashboard only (job card and jobs list, updated by polling). No email. | |
| B25 | Async runtime | Background jobs run as AgentCore Runtime async sessions (maximum 8 h, 2 vCPU / 8 GB for each session). Fargate is removed from the architecture. Keep the Job ID Store (DynamoDB) and the Polling Lambda. Add a watchdog that marks stale jobs as failed and resumes them from the last checkpoint. | The entrypoint returns at once and runs work in a background task (`add_async_task` / `complete_async_task`, ping `HealthyBusy`). Cancel = cooperative flag + `StopRuntimeSession`. Long HITL waits: checkpoint, stop the session, resume in a new session after approval. Prototype: >1 h background run, token expiry during long jobs, resume after a kill, behavior during redeploy. |
| B28 | Checkpointer | The LangGraph checkpointer is `AgentCoreMemorySaver` (AgentCore Memory short-term memory). It is used for real-time and background runs, for crash resume and HITL pause/resume. | The code exists in `shared/memory/agentcore.py` but it is not connected. It replaces `InMemorySaver`. |
| B29 | Tool agent deployment | Proposed: 1 runtime, 1 generic graph, 1 YAML profile for each source. For each request, the agent gets access only to the tools of the requested source. | Research result: this is possible and AWS enforces it with Policy in AgentCore (Cedar, GA 2026-03). See B30 (pending confirmation). |
| B30 | Tool scope enforcement | One AgentCore Gateway with all source targets and one Policy in AgentCore engine (Cedar). A Cognito resource server has one scope for each source. The Session Router holds the only M2M client secret. For each request, it gets a token with only the allowed scopes. A pre-token-generation trigger adds the user `sub` and the session ID. The agent sends the token to the Gateway without change. Tool agents get one source scope. Specialist agents get all source scopes. Policy is default-deny and filters `tools/list`. | Start in `LOG_ONLY`, then `ENFORCE`. The Gateway Lambdas use the user `sub` claim to read the user-owned API key. Prototype: `tools/list` filtering with scope tags, token TTL for long runs, denied-call error in LangGraph, and that the runtime role cannot read the M2M secret. |
| B35 | RAG | All attached documents are always indexed in the Knowledge Base and retrieved in chunks. They never go into the context in full. | |
| B36 | Memory control | Users cannot see or edit long-term memories in the MVP. This can come in a later phase. | |
| B37 | Memory strategies | Only the AgentCore Memory built-in strategies: user preferences and semantic facts. No explicit "remember" tool. Corrections are extracted by these strategies. | |
| B38 | Images and CSV | The KB uses a foundation-model parser, so PNG/JPG and CSV are also indexed. Images also go directly to vision-capable models. CSV files also go to the Code Interpreter as files. | |
| B39 | Code execution | The AgentCore Code Interpreter stays in scope (analysis, charts). Programmatic tool calling (PTC) is a later-phase spike with a safety review. | |
| B40 | Other tools in scope | Web search, the Embedded UI Generator, Life Sciences Operations tools, and the public DB search tools (the Gateway targets of B27). | Life Sciences Operations = deterministic helpers for common life-sciences operations (for example DNA→RNA, protein sequence validation). The list is TBD. |
| B42 | Life Sciences Operations | Deterministic functions with no model call, in 4 groups. (1) Sequence basics: validate DNA/RNA/protein (IUPAC rules), transcribe, reverse complement, translate (6 frames, codon tables), ORF finder, GC content, composition. (2) Protein and primer properties: MW, pI, extinction coefficient, GRAVY; primer Tm, hairpin/dimer check, restriction sites. (3) Chemistry (RDKit): validate SMILES/InChI, MW, LogP, TPSA, HBD/HBA, Lipinski Rule of 5, Tanimoto similarity. (4) Lab calculations and notation: molarity, dilution (C1V1=C2V2), unit conversion, HGVS check, FASTA/GenBank parse. | Runs as one Python Gateway Lambda target (Biopython + RDKit). RDKit is large, so the Lambda may need a container image. All agents can call it under the same Cedar scope model. |
| B43 | Embedded UI Generator | Both parts: the deterministic CEI UI presenters for normal answers, and the sandboxed model-authored Artifact (D17) for custom interactive visuals. | |
| B44 | Web search provider | Only the AgentCore Web Search built-in Gateway connector (pin connector v1.2.0). Grounding uses the returned snippets only; no page fetching. The trace stores only the URL, title and the snippets that the answer actually cited, not the full result lists (because the terms forbid bulk storage). | Available only in us-east-1, eu-west-1 and ap-northeast-1. In other Regions, `deploy.sh` turns off web search. $7 for 1,000 queries. Uses the Gateway IAM role (no API key). |
| B41 | Export formats | PDF, DOCX, Markdown, CSV, LaTeX. Slides (PPTX) are deferred and optional. | Replaces the D11 list. Remove `txt` from the protocol allowlist, or keep it (TBD). |
| B45 | Region | Configurable through environment variables and CDK context. No fixed Region. | The deploy script must check that the selected Region has all required services (AgentCore Runtime/Gateway/Memory/Policy, S3 Vectors, Bedrock KB, selected models). |
| B46 | Environments | Single environment. Developers use local AgentCore CLI runs. | |
| B47 | Deployment | A guided `deploy.sh` collects the configuration (repository, Region, blocked/allowed models, limits, allowed email domains, other settings), then starts a CodeBuild deployment. The goal is an easy, guided deployment pipeline. | |
| B48 | Agent IaC | All AgentCore resources are in CDK: runtimes (CodeZip assets), Gateway and targets, Memory, Policy engine, Knowledge Base. `agentcore.json` is for local development only. | |
| B49 | Observability | AWS-side only: AgentCore Observability (CloudWatch GenAI Observability, OTel traces) and CloudWatch dashboards. Only AWS account operators see them in the AWS console. They are not connected to the UI. | |
| B50 | Security notifications | CloudWatch alarms go to an SNS topic with an admin email (set by `deploy.sh`). | Alarm list TBD (for example 5xx, WAF blocks, Lambda errors, failed jobs, Policy denies). |
| B51 | Guardrails | Amazon Bedrock Guardrails on all agent model calls. Guardrail configuration is never shown in the trace (B7). | PII filter mode is decided in B51a. Open: the exact list of PII/PHI entity types. |
| B52 | Encryption | AWS-managed keys for DynamoDB, S3 and Secrets Manager. No customer-managed KMS key. | cdk-nag suppressions need ADR reasons. |
| B53 | Report pages | Remove the static dashboard report pages (Target Assessment, Variant Report, Regulatory Brief). They were demo mock-ups. | |
| B54 | Pipeline UI | The user asks in the chat. The Pipeline Manager proposes a plan (`request_plan_approval`). The user approves or edits it in the chat. The job then runs async, and `/pipeline` shows the live step status. No visual builder. | |
| B55 | Mode | Science/business mode is set per chat. The user selects it when the chat starts and can change it in the chat. | Replaces the global top-bar toggle in the dashboard. |
| B51a | Guardrail PII | The PII/PHI filter masks the user input only. Tool results and model output are not PII-filtered, so public NPI and author data stays. | Refines B51. |
| B56 | HITL | Real pause and resume. HITL tools use a LangGraph `interrupt`. The stream ends with the question. The checkpoint is saved (B28). The answer comes through the typed POST action and resumes the graph in a new run. | Replaces the D8 statement "does not pause or resume". |
| B57 | HITL wait | No time limit. The chat shows "waiting for you" until the user answers. | Verify the AgentCore Memory short-term event expiry (it can have a maximum retention). If there is a maximum, a waiting checkpoint must be refreshed or copied. |
| B58 | Data deletion | A user can delete a chat (history, trace, attachments, KB chunks, checkpoints) and can delete the account (all data, including long-term memories and API keys). "Forever" means "until the user deletes it". | |
| B59 | History loading | Cursor pages of turns (for example the last 20, and older turns on scroll up, with the DynamoDB `LastEvaluatedKey`). The trace of a turn loads only when the user expands its thinking block. The UI uses a virtualized list. | Closes D12 "pagination TBD". |
| B60 | Stream protocol | CEI UI 1.2 adds progressive SSE patch events (for example `surface_begin`, `component_add`, `text_delta`, `component_update`, `trace_event`, `surface_end`, `error`). The final surface is the same component graph as 1.1, so stored history stays compatible. | Event names are a proposal; the spec defines them. |
| B61 | Old decisions | Keep D16 (prompt caching). Remove D13 (monitoring). Mark D-records that B-decisions change as "superseded by Bxx". Keep `txt` in the export allowlist. | |
| B62 | Docs layout | New `.agents/` directory at the repo root for plans and specs (agent-facing). `docs/` is for human-facing documentation only. | This file moves to `.agents/`. |
| B63 | Phases | Small, feature-scoped phases: spikes S1–S5 and features F1–F21 (see the Phase list below). | Details are in `.agents/roadmap.md` and `.agents/phases/`. |
| B64 | `.agents/` layout | `decision-log.md`, `roadmap.md`, `specs/` (architecture, data-model, api, streaming-protocol, auth-and-tool-scope, agents, tools, security), `phases/` (one file per phase). | |
| B65 | Human docs now | Before the build, `docs/` describes the target state, and each item is clearly marked "planned / not built". `userGuide` and `deploymentGuide` stay outlines until the features exist. `provisioned-resources.md` must list only what is really deployed. | |
| B66 | Repo layout | The dashboard code replaces `frontend/`. The old Next.js code moves to `archive/frontend-next/` for reference (for example the CEI UI renderer and zod schema). | |
| B67 | Token refresh | Tool tokens live 1 h. Before expiry, a running agent calls an internal token-refresh endpoint (IAM auth). The endpoint checks that the run/job is still active (Job ID Store or active-run record) and returns a new token with the same scopes and user. The agent never has the M2M secret. | Resolves the token-TTL TBD in auth-and-tool-scope.md. |
| B68 | Stream reconnect | The Session Runner writes each SSE event with a sequence number to the Chat History Store. After a disconnect, the client reconnects with its last sequence number. The server replays the missing events from DynamoDB, then continues live. | Replaces the "Runner in-memory buffer" proposal in streaming-protocol.md. |
| B69 | Cedar granularity | One Cedar permit for each source target (all tools of that target). | |
| B70 | Shared tools scope | One shared scope (for example `cei-tools/common`) for Life Sciences Ops, Code Interpreter, Web Search and KB retrieval. All agents and tool agents always get it. | |
| B71 | Drafting research | The Drafting Agent keeps its research subgraph in-process (same runtime and session), using the Gateway tools. It does not call the Research runtime. | |
| B72 | HITL points | Research: a clarifying question when triage finds no suitable source or the request is ambiguous. Drafting: outline approval before the full draft, and revision requests through an interrupt that resumes into the revise loop. Analysis: scope confirmation (competitors, market, jurisdiction) before the analysis. All agents: can ask the user questions (multiple choice and long free-text answers). | |
| B73 | Delete API | Delete chat and delete account return 202 at once and mark the item "deleting". A worker removes the DynamoDB items, S3 files, KB chunks, memories and checkpoints. The UI hides the item at once. | |
| B74 | Export triggers | Both: the agent can produce exports during a run, and each completed answer has an Export action (`POST /sessions/{id}/turns/{turn}/export`) with the D11 review acknowledgment. | |
| B75 | Alarm list | SNS email alarms for: (1) API and Lambda errors: API Gateway 5xx rate, Lambda errors/throttles, DynamoDB throttling. (2) Security events: WAF block spikes, Cedar Policy denies, sign-up spikes. (3) Guardrail intervention spikes. No agent-health alarms (stale jobs are handled by the watchdog and shown in the dashboard). | Resolves the B50 open item. |
| B76 | Per-user models | No per-user model restriction. Only the global blocklist (B18). | |
| B77 | KB retrieval | KB retrieval is a Gateway Lambda target. The Lambda adds the `user_id` + `session_id` filter from the token claims. The agent cannot change the filter. | |
| B78 | Defaults | History page 20 turns (max 100). Trace chunk 350 KB. `text_delta` chunk max 4 KB. Secret names `cei/user-keys/{user_id}/{source_id}`. Export download URL TTL 15 min. Only the global 20 rps throttle (no per-route throttle). | Resolves the matching spec proposals. |
| B79 | Async threshold | For non-pipeline runs, if the plan estimates more than 10 minutes, the run goes async. This leaves a 5-minute margin under the 15-minute stream limit. There is no mid-run handoff. | Resolves the B23 open item. |
| B80 | Router low confidence | When the free-text classifier confidence is low, the Router asks the user with a ChoicePicker ("Which kind of help do you want?") that lists the 4 agents. The answer routes the request. | Confidence threshold value: implementation default, tune in F9. |
| B81 | Review gate UI | The export dialog has an "I have reviewed this output" checkbox. The Export button is disabled until the user checks it. The acknowledgment is logged. | Implements D11. |
| B82 | HITL expiry | AgentCore Memory short-term events use the maximum event expiry (verify the value; expected 365 days). After that, a waiting HITL question shows "expired, start again". | Refines B57. |
| B83 | Pipeline dispatch | Each Pipeline Manager step calls the specialist agent runtime (`InvokeAgentRuntime`). Concurrent steps run in separate sessions (each with its own 2 vCPU / 8 GB) and have their own trace. | |
| B84 | Tool agent route | Tool agent chats use a separate streaming route `/tools/{source}/chat/stream` (STREAM mode, same Session Runner). The Router checks `source` and issues the scoped token. | |
| B85 | NCBI access | Specialist agents also get the NCBI scope (BLAST, taxonomy, sequence), like all other sources. NCBI is not tool-agent only. | |
| B86 | Scope names | Canonical scope names: `cei-tools/src.<source>` for each source (pubmed, clinicaltrials, biorxiv, medrxiv, chembl, opentargets, cmscoverage, icd10, npi, pubchem, ncbi) and `cei-tools/common` for shared tools. The Web Search connector is a Gateway built-in connector target, so Cedar controls it under `cei-tools/common`. The Code Interpreter is not a Gateway target, so the runtime binding controls it. | Cedar syntax in the specs is illustrative. S2 confirms the exact tag/claim syntax. |
| B31 | Attachment scope | Attachments are per chat. A file is available only in the chat where the user uploaded it. | |
| B32 | File types | PDF, DOCX, TXT, MD, CSV, PNG, JPG. Maximum 55 MB (presigned POST with a content-length-range condition). | CSV also goes to the Code Interpreter. PNG/JPG go to vision-capable models. |
| B33 | Vector store | Bedrock Knowledge Base with S3 Vectors. Each chunk has `user_id`, `session_id` and `file_id` metadata. Every retrieval uses a metadata filter. | |
| B34 | Long-term memory | AgentCore Memory long-term strategies remember, across chats: user preferences, semantic facts about the user's work, and specific corrections or items that the user asks the agent to remember. | |
| B22 | Limit values | User-sent requests: 20 requests/s. Concurrency for each user: a maximum of 2 active real-time runs and 1 active background run. Agent-to-tool calls are not throttled by us. | Upstream API limits still apply (for example NCBI: 3 rps without a key, 10 rps with a key). Tool Lambdas need retry with backoff. |
| B15 | Routing | Task-specific routing: a dashboard task maps to its agent through a deterministic lookup in the Session Router. Free-text chat goes to a router classifier that can select any of the 4 agents (Research, Drafting, Analysis, Pipeline Manager). | Changes D1 ("no model call") for free-text chats only. |
| B87 | Deploy mechanism | `deploy.sh` creates the CodeBuild IAM role `cei-deploy-codebuild-role` and the CodeBuild project `cei-life-sciences-deploy` with the AWS CLI. There is no CDK pipeline stack. `BackendStack` stays the only product stack. CodeBuild clones the GitHub repository at the given URL and branch. For a private repository, the user gives a GitHub PAT. `deploy.sh` stores it in Secrets Manager as `cei/deploy/github-pat` and attaches it to the CodeBuild project only, through `source.auth` (`SECRETS_MANAGER`) — never account-wide. The Region pre-flight check is TypeScript (`backend/scripts/preflight.ts`); `deploy.sh` runs it locally, and CodeBuild runs it again in `pre_build`. | SSM public service IDs check service-level availability only. Feature gaps (for example, AgentCore Memory is missing in us-west-1, eu-south-1, eu-south-2, ap-southeast-5, ap-south-2, ap-southeast-7, checked 2026-09-29) come from a hardcoded matrix in `backend/lib/config/region-capabilities.ts`. Refines B45/B47. |

## Phase list

| # | Phase | Contents | Depends on |
|---|---|---|---|
| S1 | Spike: streaming | REST API response streaming + Cognito authorizer + CloudFront (heartbeat, timeouts), Node.js Session Runner relaying `InvokeAgentRuntime` chunks | — |
| S2 | Spike: tool scope | Cedar Policy on Gateway + Cognito M2M scoped tokens; `tools/list` filtering; denied-call behavior | — |
| S3 | Spike: async | AgentCore async session > 1 h (`HealthyBusy`), `StopRuntimeSession` cancel, `AgentCoreMemorySaver` resume after kill, token expiry | — |
| S4 | Spike: HITL | LangGraph `interrupt` + checkpoint + resume in a new run through a typed action | — |
| S5 | Spike: RAG | Bedrock KB with S3 Vectors, FM parser for PNG/JPG/CSV, metadata filters | — |
| F1 | Deploy foundation | `deploy.sh`, CodeBuild, CDK skeleton, configuration (Region, models, limits), cdk-nag | — |
| F2 | Hosting + auth | Cognito (self sign-up, domain allowlist), S3 + CloudFront + WAF, dashboard port into `frontend/` with login | F1 |
| F3 | API + session store | REST API, authorizer, throttling, Session Metadata + Chat History tables, session list/history handlers, pagination | F2 |
| F4 | Data tools | Gateway, 10 source Lambdas, Cognito scopes, Cedar Policy, M2M tokens | F1, S2 |
| F5 | Research agent live | Research runtime in CDK, deterministic Router, Session Runner streaming, concurrency limit | F3, F4, S1 |
| F6 | Streaming UI | CEI UI 1.2, renderer port, thinking block/trace, grounding pending state, trace storage (compress + split) | F5 |
| F7 | Drafting + export | Drafting runtime, PDF/DOCX/MD/CSV/LaTeX/TXT export | F6 |
| F8 | Model + mode controls | Model picker + blocklist, per-chat mode, Guardrails, prompt caching | F6 |
| F9 | Free-text router | Classifier model in the Router Lambda | F5 |
| F10 | Attachments + RAG | Upload, SQS, processor, KB, image/CSV direct use | F6, S5 |
| F11 | Long-term memory | AgentCore Memory strategies | F5 |
| F12 | Web search + LS ops | Web Search connector, Life Sciences Ops Lambda | F4 |
| F13 | HITL | Pause/resume in all agents, typed action endpoint | F6, S4 |
| F14 | Analysis agent | Analysis runtime | F6, F13 (scope-confirm HITL, B72) |
| F15 | Tool agents | 1 runtime + 11 profiles, NCBI targets | F4, F6 |
| F16 | Background jobs | Job ID Store, Polling Lambda, watchdog, cancel | F5, S3 |
| F17 | Pipeline Manager | Pipeline runtime, plan approval, `/pipeline` job view | F13, F16 |
| F18 | User API keys | Settings page, Secrets Manager, key use in Lambdas | F4 |
| F19 | Data deletion | Delete chat, delete account | F10, F11 |
| F20 | Alarms | CloudWatch alarms, SNS email, dashboards | F5 |
| F21 | Hardening | Security review, load test, human docs complete | all |
| Later | Deferred | PTC, slides export, paid sources, memory UI | — |

## Still open (record as TBD in the specs)

Product decisions (need the user):
- Design of user-configured paid sources (deferred by the user).
- Product name and branding.

To confirm in spikes (S1–S5):
- AgentCore Memory short-term event expiry: the exact maximum value (B82 decides the behavior).
- Exact Cedar claim/tag syntax and action names, including the Web Search target (S2, B86).
- `tools/list` filtering with scope tags, and the error shape of a denied call in LangGraph (S2).
- Router → Runtime token transport: an allowlisted custom header or `Authorization` (S1/S2).

Implementation details (the phase owner selects a value and records it here):
- Trace and SSE-replay item key schema (B9, B68; chunk size is decided in B78).
- Router low-confidence threshold value (B80, tune in F9).
- Guardrail PII/PHI entity type list (B51a).
- `usage` event cadence, grounding pending minimum duration, surface `revision > 1` rules.
- Active-run counter table name and leak recovery. Export bucket name/key scheme. Deletion worker fan-out. User Metadata email copy vs. live Cognito read.
- Development CORS origins. Production CloudFront CSP content and how it interacts with the Artifact sandbox CSP.
- Analysis `compare` framework and scope-confirm UI (F14). Tool-agent long-term memory eligibility (F11/F15). BLAST poll interval (F15). Token refresh cadence (F16).
