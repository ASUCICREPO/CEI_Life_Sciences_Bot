# Agent Architecture Decisions

Decisions made during the planning phase for the CEI Life Sciences Bot AgentCore agent.

---

## D1: Agent Topology — Multi-Agent with Manager

**Decision:** Multiple specialized AgentCore agents coordinated by a manager.

**Structure:**
- **Research Agent** — Goal 1: deep research across 10 data sources. Triages queries, retrieves evidence, tags sources, surfaces conflicts and gaps.
- **Drafting Agent** — Goal 2: document preparation for grants, VCs, journals, whitepapers. Researches latest guidelines, finds best-fit opportunities.
- **Analysis Agent** — Goal 3: competitor analysis, market analysis, legal landscape. Mode-aware output (SWOT/financials for business, competing research/frameworks for science).
- **Pipeline Manager Agent** — Goal 4: orchestrates the other agents in concurrent, sequential, and looped tasks. Presents a plan for user approval. Monitors step status and handles failures.

**Routing:** Frontend routes directly to the correct specialist for Goals 1–3 based on task type (already classified in the task launcher UI). The Pipeline Manager handles Goal 4 only. A lightweight Session Router Lambda (no LLM call) maps task type metadata to the correct AgentCore agent — fast and deterministic, no wasted tokens.

---

## D2: Data Source Integration — Lambda Tools via AgentCore Gateway

**Decision:** Each of the 10 data sources is a Lambda function behind AgentCore Gateway.

**10 Data Sources (current scope):**

| # | Source | Type |
|---|---|---|
| 1 | PubMed | Biomedical citations and abstracts |
| 2 | ClinicalTrials.gov | Study records, status, sponsors, results |
| 3 | bioRxiv | Life-science preprints |
| 4 | medRxiv | Health-science preprints |
| 5 | ChEMBL | Bioactive molecules, biological targets |
| 6 | Open Targets | Drug target–disease evidence |
| 7 | CMS Coverage Database | Medicare coverage policies |
| 8 | ICD-10 | Disease/condition classification codes |
| 9 | NPI Registry | Healthcare provider identifiers |
| 10 | PubChem | Chemical structures, properties, bioactivity |

**Why AgentCore Gateway:** Governed access with auth and policy enforcement per tool. Centralized logging. Each Lambda is independently deployable and testable.

**API Key Policy:** User-owned keys only. Open-access sources (no key required) are used freely. Sources that require API keys require the user to provide their own, stored in Secrets Manager per user.

---

## D3: AI Provider — Bedrock Only, User-Selectable Model with Blocklist

**Decision:** All inference runs through Amazon Bedrock. Users select their preferred model per chat from dropdown menus. Admins/developers control which models are available to users.

**Why:** AgentCore supports Bedrock model IDs natively. No custom inference routing needed. Simplest architecture.

**Model blocklist (never available in the application):**
- Claude Fable 5.1, Fable 5 (`anthropic.claude-fable-5*`)
- Claude Mythos 5.1, Mythos 5, Mythos Preview (`anthropic.claude-mythos-*`)

**Explicitly allowed (non-obvious):**
- GPT-5.6 Sol (`openai.gpt-5.6-sol`)

---

## D16: Prompt Caching

**Decision:** Enable explicit prompt caching for all eligible models to reduce cost on repeated system prompts and tool definitions.

**How:** Place cache checkpoints on static content (system prompt, tool definitions) in every inference call. Dynamic content (user messages, conversation history) goes after the checkpoints.

**Eligible models in our allowlist:**
| Model | Min tokens per checkpoint | Max checkpoints | TTL |
|---|---|---|---|
| Claude Opus 5, 4.8, 4.7, 4.6, 4.5 | 512–4096 | 4 | 5 min (or 1 hr) |
| Claude Sonnet 5, 4.6, 4.5, 3.7, 3.5v2 | 1024 | 4 | 5 min (or 1 hr) |
| Claude Haiku 4.5 | 4096 | 4 | 5 min (or 1 hr) |
| GPT-5.6 Sol | 1024 | 4 | 30 min |
| Amazon Nova | Implicit only (automatic, no config) |

**Cost impact:** Cache reads cost ~90% less than standard input tokens. Cache writes cost ~1.25× uncached rate. Cached tokens do not count against input-tokens-per-minute quota.

**AgentCore integration:** Prompt caching is configured at the Converse/InvokeModel API level (`cachePoint` objects in request body). Whether AgentCore exposes this natively needs testing with the CLI. If not, we may need to configure it in the agent's model invocation settings.

**Caching strategy:**
1. System prompt (per agent, per mode) — cached (large, static)
2. Tool definitions (10 data source schemas + inline functions) — cached (static per session)
3. Conversation history — not cached (dynamic)

---

## D4: Science / Business Mode — Full Agent Behavior Change

**Decision:** The mode toggle changes the agent's system prompt, tone, domain focus, output format, and audience targeting.

- **Science mode:** Emphasizes raw evidence, methodologies, statistical rigor, research comparisons, experimental details.
- **Business mode:** Emphasizes market implications, competitive positioning, executive summaries, financial breakdowns, user profiles.

Each specialist agent has two system prompt variants. The mode is passed as metadata in every request.

---

## D5: Citation Grounding — Tool-Level Enforcement

**Decision:** Every data source Lambda returns structured citations with each result.

**Citation structure (returned by every tool):**
```json
{
  "sourceId": "PMID:12345678",
  "sourceType": "pubmed",
  "url": "https://pubmed.ncbi.nlm.nih.gov/12345678/",
  "title": "...",
  "authors": ["..."],
  "year": 2024,
  "relevantPassage": "...",
  "retrievedAt": "2026-09-14T..."
}
```

**Agent system prompt rule:** Every factual claim must reference a tool-returned citation. Claims without citations must be flagged as assumptions or estimates. The agent must surface conflicts when sources disagree and flag missing data explicitly.

---

## D6: Agent-to-UI Protocol - CEI UI

**Decision:** Use the custom CEI UI protocol for rich agent responses. The content type is `application/vnd.cei.ui+json`, the protocol ID is `cei-ui`, and the response field is `ui`. The protocol is now at version 1.1.

**Scope for this phase:**
- A completed response contains one flat, allowlisted component graph.
- Deterministic presenters create CEI UI from typed agent state. Models do not create component records, arbitrary UI, or JavaScript. The one exception is `Artifact.html`, which is model-authored but never executes outside a sandboxed iframe (see the "Sandboxed model-authored artifacts" ADR below).
- The first transport is completed JSON. Typed POST actions are part of the contract.
- Final SSE surfaces and progressive patches are deferred.
- Plain text is required as a fallback: when `ui` fails validation, the client drops it and shows `text` with a notice, and the local proxy omits `ui` and sets `ui_error`.

**1.1 addition:** Six new leaf components — `Table`, `StatGrid`, `Callout`, `Timeline`, `Comparison`, `Artifact` — so research and drafting answers can be rendered as tables, metric tiles, comparisons, timelines, callouts, and sandboxed interactive artifacts instead of one Markdown block. 1.1 is additive: servers emit `"1.1"`; clients and stored chat history continue to accept `"1.0"`.

**Current status:** CEI UI 1.1 schemas, deterministic presenters, completed response surfaces, frontend validation and rendering, and local typed action dispatch are implemented. The local Next.js proxy routes are disabled unless `CEI_ENABLE_LOCAL_PROXY=true`. They are for preview only because the production frontend target is a static S3 and CloudFront deployment. Production authentication, Session Runner routing, SSE delivery, and graph resume are not implemented.

**Rationale:** A small CEI-owned contract gives the application a stable allowlist, direct validation, typed actions, and no dependency on executable model-generated UI beyond the sandboxed artifact exception. See [CEI UI Protocol 1.1](./cei-ui-protocol.md).

---

## D7: Typed Presentation Output - ResponseDocument Blocks + CEI UI

**Decision:** The research and drafting agents produce a typed `ResponseDocument` (`backend/agents/shared/schemas/response_blocks.py`) through a structured tool call: a title, summary, up to four key metrics, and one to twelve sections of typed presentation blocks (`prose`, `table`, `metrics`, `chart`, `comparison`, `callout`, `timeline`, `artifact`). Deterministic presenters map these blocks to CEI UI surfaces.

**Flow:** An agent submits a `ResponseDocument`. `shared/grounding.py` checks every non-prose block's numbers against an evidence corpus and sets `verification` server-side (the model never sets it). A presenter maps the grounded blocks to an allowlisted CEI UI graph and returns it in the completed JSON response. Persistence, export storage, and streaming delivery apply only when their infrastructure is implemented.

**Note:** The domain-specific typed outputs originally described here (`TargetAssessment`, `VariantReport`, `RegulatoryBrief`, `typed_outputs.py`) are not used for presentation. `ResponseDocument` blocks are the one presentation-facing typed contract for research and drafting; any future domain-typed outputs would still need to go through a presenter to reach CEI UI.

---

## D8: Human-in-the-Loop - Typed Actions + CEI UI

**Decision:** Keep the intent of typed inline-function prompts for HITL interactions. Render each prompt as a CEI UI component and return each user response as a typed POST action.

**Inline function tools:**
- `ask_user_question` - present a question with a `ChoicePicker`
- `present_alternatives` - show options with a `Card` list
- `request_plan_approval` - show a plan with a `ReviewGate`
- `get_user_input` - request text with a `TextField`

**Target flow:**
1. The agent requests a typed interaction.
2. The presenter creates a CEI UI surface with a typed action descriptor.
3. The client sends the action by POST with the session, surface, revision, component, and interaction IDs.
4. The server validates the action and returns `accepted`, `completed`, `stale`, `rejected`, or `error`.
5. When pause and resume support exists, the server resumes the saved graph with the validated value.

**Current limitation:** The current helpers return HITL UI data, and the local preview can submit and validate typed actions through its opt-in proxy. The repository has no production action endpoint, Session Runner, or actual LangGraph pause and resume flow.

---

## D9: Synchronous vs Async Execution

**Decision:** Two execution paths through one HTTP API Gateway.

| Path | Route Type | Compute | Timeout | Use Case |
|---|---|---|---|---|
| **Streaming (SSE)** | `/chat/stream` | Session Runner Lambda → AgentCore Runtime | 15 min (API Gateway limit) | Real-time chat, research queries, document editing, HITL interactions |
| **Async (background)** | REST routes | Lambda → Fargate task | Hours | Deep research, pipeline orchestration (Goal 4), long-running analysis |

**Async flow:**
1. Client POSTs to create a pipeline/task → gets task ID + estimated duration
2. Fargate task runs a copy of the agent with the same tools
3. Client polls `/api/tasks/:id/status` or subscribes to `/api/tasks/:id/stream` for incremental updates
4. Partial results returned even if some branches fail

**Development/Testing:** Agent development and testing uses the AgentCore CLI directly — no infrastructure provisioning (no API Gateway, Lambda, Fargate, or CDK deploy) required during the agent-building phase. Infrastructure is provisioned later when integrating with the frontend.

---

## D10: Pipeline Orchestration — Plan Approval + Self-Healing

**Decision:** The Pipeline Manager Agent (Goal 4) follows this flow:

1. **Plan generation:** Agent generates a step-by-step plan with estimated durations and dependencies
2. **User review:** Plan presented with CEI UI components. User can comment on parts and refine
3. **Approval gate:** User must explicitly approve before execution starts
4. **Execution:** Manager launches concurrent, sequential, and looped sub-tasks on Fargate
5. **Step monitoring:** Each step reports status (idle/running/completed/partial/failed)
6. **Failure handling:**
   - Retry with exponential backoff on the failed tool
   - Try alternative tools if the primary tool keeps failing
   - If all self-heal options are exhausted: surface the cause to the user with options to Retry, Skip and continue, or provide free-response guidance to steer the agent
7. **Synthesis:** When all branches complete, generate a unified result with risks, open questions, and next steps

---

## D11: Export — Soft Acknowledgment Gate

**Decision:** Soft acknowledgment before export. User confirms "I have reviewed this for accuracy" before exporting. The acknowledgment is logged.

**Export formats (from API contract):** PDF, slides, CSV, Markdown, DOCX.

**Confidentiality levels:** Internal only, Shareable with partners, Public.

---

## D12: Session Persistence — Indefinite

**Decision:** Sessions persist indefinitely until the user deletes them. No TTL. Chat History Store in DynamoDB. Lazy loading required on the frontend to prevent browser crashes on large histories (pagination strategy TBD).

---

## D13: Monitoring — User-Triggered Only

**Decision:** No automatic scheduled monitoring (Goal 3). User manually triggers monitoring checks. No EventBridge rules or background scheduling infrastructure needed.

---

## D14: Alerts — In-Dashboard Only

**Decision:** Monitoring alerts appear in the dashboard when the user logs in. No email, Slack, or webhook notifications.

---

## D15: Collaboration — Single-User

**Decision:** Single-user system. Each user has their own sessions, exports, and settings. No shared workspaces, team review, or multi-user features.

---

## D17: Sandboxed Model-Authored Artifacts

**Context:** Research and drafting answers needed an interactive view (for example, a dose-response slider) to look like a Claude artifact. Every other CEI UI component is built by a deterministic presenter from typed data; the model never writes a component record. An interactive view is different: the visualization logic itself — not just the data — has to come from the model, because the presenter cannot anticipate every interaction a user might want.

**Decision:** Add one component, `Artifact` (wire) / `ArtifactBlock` (model-facing), that carries model-authored `html` (vanilla JS, SVG, or canvas; a body fragment only) plus an optional `data` table. This is the only place in CEI UI where model output becomes executable code. It runs only inside a sandboxed iframe with `sandbox="allow-scripts"`, a host-built `srcDoc`, a restrictive CSP `<meta>` tag, a single whitelisted resize message, and a navigation tripwire that unmounts the frame on any second load. See "Artifact Security" in [CEI UI Protocol 1.1](./cei-ui-protocol.md) for the full mechanism. A server-side and client-side denylist lint (`ARTIFACT_HTML_DENYLIST`) rejects obvious network, navigation, storage, and frame-escape patterns before a surface is ever produced, as defense in depth — not as the security boundary.

**Alternatives considered and rejected:**
- **Typed-only, no artifact.** Keep every CEI UI component fully typed and presenter-built, with no model-authored code at all. Rejected: this cannot produce a genuinely interactive, model-directed view (for example, a slider whose behavior depends on what the model decided to show); the "Claude artifact" goal requires some model-authored logic.
- **Server-rendered images.** Have the model describe a chart and render it server-side to a static image (PNG/SVG) instead of shipping live HTML/JS. Rejected: static images cannot be interactive, and this was already the drafting agent's chart approach pre-1.1 (base64 PNGs), which is exactly what this work moves away from.
- **`allow-same-origin` on the iframe.** Would let artifact scripts read `document.domain`-adjacent state and simplify some debugging. Rejected: combined with `allow-scripts`, `allow-same-origin` lets a sandboxed frame access the embedding page's origin, defeating the sandbox; the two tokens must never be combined for untrusted content.
- **A vendored chart library inside the sandbox.** Would give the model a safer, more consistent way to draw charts. Rejected for now: it adds a trusted, audited dependency inside the sandbox and expands the reviewable surface; artifacts are vanilla JS/SVG/canvas only until a specific need justifies the added dependency.

**Consequences:**
- The prompt does not limit how many blocks or artifacts an answer uses; it directs the model to prefer graphics over prose. The protocol still caps each artifact at 64 KiB `html` and 64 KiB `data`, with a combined per-surface cap of 262,144 bytes and at most 4 artifacts per surface. The presenter degrades artifacts past the fourth to a Callout plus a data Table.
- The frontend needs a dedicated `ArtifactFrame` component, a resize protocol, and a navigation tripwire; this is meaningfully more client complexity than any other CEI UI component.

**Residual risk:** The sandbox (no `allow-same-origin`) plus CSP is the boundary, not the denylist lint. Three risks remain even with the sandbox correctly configured: (1) a sandboxed frame can still attempt one self-navigation before the tripwire's second `onLoad` fires, which could leak a small amount of artifact-local data in that single outgoing navigation; (2) `srcDoc` documents inherit the parent page's CSP in addition to their own `<meta>` CSP, so a future production CloudFront/CDN CSP header must deliberately accommodate the artifact's CSP rather than silently conflict with it; (3) the `<meta>` CSP cannot enforce `frame-ancestors`, so framing of the app itself must be controlled at the top level, not inside the artifact.

**Kill switch:** `NEXT_PUBLIC_CEI_ENABLE_ARTIFACTS` can disable artifact execution entirely without a schema change. When it is off, the frontend renders only the artifact's description and data table, never the `html`.

**Status:** Implemented.

---

## Open Questions (Remaining)

These open questions from the prototype have not been resolved and do not block agent architecture:

| # | Question | Category |
|---|---|---|
| 1 | Full CEI Gateway logo or separate product identity? | Branding |
| 2 | Product name? | Branding |
| 3 | Formal brand guidelines or derive from website? | Branding |
| 8 | Mock cost estimates and approval modals in prototype? | Prototype |
| 9 | Patent/FTO: private patent vector DB or public sources only? | Data sources |
| 10 | Windows client support? | Platform |
| 11 | Additional stakeholder reviewers? | Process |
| 12 | Onboarding flow: every new user or first visit? Re-runnable? | UX |
| 14 | Max concurrent sessions per user? | Architecture |
| 19 | Demo scenarios: generic or actual portfolio companies? | Demo |
| 20 | Include all verticals (diagnostics, devices, therapeutics)? | Demo |
| 21 | Compute-gated task representation? | UX |
| 22 | Is prototype the Week 1 or Week 4 deliverable? | Timeline |
| 23 | Engineering handoff recipient? | Process |
| 24 | Component library constraints beyond React/TS/Vite/Tailwind? | Frontend |
