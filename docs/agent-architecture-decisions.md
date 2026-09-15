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

## D3: AI Provider — Bedrock Only, User-Selectable Model

**Decision:** All inference runs through Amazon Bedrock. Users select their preferred model per chat from dropdown menus populated with all Bedrock-available models (Claude, Nova, Llama, Mistral, etc.). Admins/developers control which models are available to users.

**Why:** AgentCore supports Bedrock model IDs natively. No custom inference routing needed. Simplest architecture.

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

## D6: Agent-to-UI Protocol — A2UI (Google)

**Decision:** Adopt the A2UI specification (v1.0, Apache 2.0, `google/A2UI`) for agent-to-UI rendering.

**How it works:**
- Agent emits A2UI JSON (`application/a2ui+json`) as structured tool output or SSE event payload
- Frontend uses the official A2UI React renderer for standard components (Text, Card, List, Button, ChoicePicker, TextField, Tabs, Modal, etc.)
- Domain-specific components (CitationChip, ConfidenceBadge, EvidencePanel, ProvenanceTrail, ReviewGate) registered as custom catalog entries
- Catalog-based security: frontend advertises which component types it trusts
- Data binding via JSON Pointer paths allows agent to update data without resending the component tree
- Streaming-friendly: flat component structure renders incrementally as the agent generates

**18 built-in A2UI components:** Text, Image, Icon, Video, AudioPlayer, Divider, Row, Column, List, Card, Tabs, Modal, Button, TextField, CheckBox, ChoicePicker, Slider, DateTimeInput.

---

## D7: Typed Structured Output — AgentCore Tool Output + A2UI

**Decision:** Agent produces typed outputs (TargetAssessment, VariantReport, RegulatoryBrief, etc.) via structured tool calls with JSON schemas. These are also rendered as A2UI component trees for the frontend.

**Flow:** Agent calls a tool like `submit_target_assessment` with a typed JSON schema → the result is stored in DynamoDB → the agent also emits A2UI components for real-time rendering → frontend renders both the streaming A2UI and stores the structured data for export.

---

## D8: Human-in-the-Loop — AgentCore Inline Functions + A2UI

**Decision:** Use AgentCore's `inline_function` tool type for all HITL interactions. Render HITL prompts as A2UI components.

**Inline function tools:**
- `ask_user_question` — present a question with options (rendered as A2UI ChoicePicker)
- `present_alternatives` — show multiple options with descriptions (rendered as A2UI Card list)
- `request_plan_approval` — show a pipeline plan for review (rendered as A2UI Card + Button)
- `get_user_input` — free-text input (rendered as A2UI TextField)

**Flow:**
1. Agent calls an inline function (e.g., `ask_user_question`)
2. AgentCore pauses execution, streams the tool call back to Session Runner Lambda
3. Session Runner emits an SSE `hitl_request` event with A2UI JSON to the frontend
4. Frontend renders the A2UI components (interactive form/options)
5. User responds → frontend POSTs to REST endpoint → Session Runner re-invokes the harness with the tool result
6. Agent resumes with the user's input

**Key property:** AgentCore does not persist the inline function turn to the session until resolved. If the user never responds, the session stays clean.

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

---

## D10: Pipeline Orchestration — Plan Approval + Self-Healing

**Decision:** The Pipeline Manager Agent (Goal 4) follows this flow:

1. **Plan generation:** Agent generates a step-by-step plan with estimated durations and dependencies
2. **User review:** Plan presented via A2UI components. User can comment on parts and refine
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
