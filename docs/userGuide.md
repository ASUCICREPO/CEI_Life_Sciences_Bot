# User Guide

Status: **Partly built.** The frontend, Cognito sign-in, and the REST API exist in CDK (see [Provisioned Resources](./provisioned-resources.md)). Each journey below has its own status: **Built**, **Partly built**, or **Planned**. This guide will get real screenshots and step-by-step instructions when each journey is verified on a deployment. Full detail: `.agents/specs/architecture.md`, `.agents/specs/api.md`, `.agents/decision-log.md`.

---

## Introduction

CEI Life Sciences Bot will be an AI-powered assistant for life sciences research, drafting, analysis, and multi-step pipelines, backed by ten public data sources plus NCBI tools, and by a user's own documents.

### Planned key features

- Deep research with citation grounding across 10+ life sciences data sources
- Document drafting (grants, VC decks, journal articles, whitepapers) with export to PDF/DOCX/Markdown/CSV/LaTeX
- Competitor, market, and legal landscape analysis
- Multi-agent pipelines with plan approval and live progress
- A collapsible "thinking block" showing the agent's live reasoning and tool calls
- Chat attachments retrieved through the Knowledge Base
- Human-in-the-loop questions the agent can ask mid-task
- Background jobs for long-running work, visible in a jobs list and a `/pipeline` view

---

## Journey: Sign up (Built)

1. User opens the application URL and selects "Sign up."
2. User enters an email address and password. Cognito enforces an email-domain allowlist set by the deployment; sign-up from a disallowed domain is rejected before verification.
3. User verifies their email address.
4. There is no MFA step (B14).

![Sign up](./media/step-1-signup.png)

> **[PLACEHOLDER]** Screenshot of the sign-up screen.

---

## Journey: Start a chat (Partly built)

A user can start a chat in two ways:

- **Pick a task** from a task launcher (a specific research, drafting, or analysis task). This routes deterministically to the matching specialist agent — no model call decides the routing.
- **Type free text** (F9: the backend is built; the free-text input in the chat screen is in progress). A small, fast classifier model picks the best-fit specialist agent (Research, Drafting, Analysis, or Pipeline Manager) automatically. If the system is not sure which agent fits, it asks the user to pick one from a short list of the four agents instead of guessing.

When the list of four agents shows:

1. Select the agent that fits your request.
2. Select **Choose**.
3. The chosen agent starts with your original message. You do not type it again.

The list stays valid for 10 minutes. After that, the chat shows "This request has expired. Start again." Send your message again.

![Start a chat](./media/step-2-start-chat.png)

> **[PLACEHOLDER]** Screenshot of the chat start screen.

---

## Journey: Per-chat mode and model (Partly built)

Each chat has a **mode** (science or business) chosen when the chat starts, changeable at any point in that chat. The mode control exists in the chat view. Each chat also has a **model picker** (F8, built). The list shows the available Bedrock models. Models that the deployment blocks are not in the list.

To select a model for a chat:

1. Start a new chat.
2. Before you send the first message, open the **Model** list.
3. Select a model. Or keep **Default** to use the deployment default model.
4. Send your first message.

After the chat starts, the model is locked. The control shows "The model is fixed when the chat starts." To use a different model, start a new chat. Some models in the list can fail at run time, because the deployment can run Anthropic models only.

![Mode and model picker](./media/step-3-mode-model.png)

> **[PLACEHOLDER]** Screenshot of the mode/model controls, once built.

---

## Journey: Watching an answer build (thinking block) (Partly built)

The thinking block and the streaming surface exist in the frontend (`frontend/src/components/streaming/`). Today only Research streams its answer (CEI UI 1.2), and only when the deployment has `researchStreamingEnabled`. The other agents show the completed answer at the end. When the deployment turns on web search (F12, off by default), Research can also search the web and cite web pages.

While the agent works, the user sees:
- A live step status (for example "Searching PubMed").
- A collapsible **thinking block** showing tool calls, sources accessed, and the model's reasoning text, expanding automatically while the agent works and collapsing once the answer is ready. The user can scroll up and re-expand it later.
- A live token/cost counter.
- The answer itself streaming in, piece by piece, with a "verifying citations" state on tables/charts/statistics until grounding finishes.

![Thinking block](./media/step-4-thinking-block.png)

> **[PLACEHOLDER]** Screenshot of an in-progress answer with the thinking block expanded.

---

## Planned journey: Attachments (not built)

1. User uploads a file (PDF, DOCX, TXT, MD, CSV, PNG, or JPG; up to 55 MB) into a specific chat.
2. The file becomes available only within that chat.
3. The agent retrieves relevant chunks from the file automatically as part of its research, rather than reading the whole file into context. CSV files are also available to the Code Interpreter; images are also available directly to models that support vision.

![Attachments](./media/step-5-attachments.png)

> **[PLACEHOLDER]** Screenshot of the attachment upload UI, once built.

---

## Journey: Answering agent questions (HITL) (Partly built)

Four agents pause for an answer (F13, built; not yet verified on AWS):

- **Research** asks one clarifying question when no data source fits, or when the request can mean more than one thing.
- **Drafting** asks you to approve the outline before it writes the full draft. Drafting does not pause again after the draft. To change the draft, send a new message in the same chat.
- **Analysis** asks you to confirm the scope.
- **Pipeline Manager** asks you to approve the plan. The steps inside a pipeline do not ask the Research or Drafting questions.

To answer a question:

1. Read the question card in the chat.
2. Select an option, or type your answer.
3. Submit the answer. The agent continues from the point where it stopped.

The question card also shows on a streamed Research answer.

Some agents pause mid-task to ask the user a question — for example, Research asking for clarification when no data source clearly fits, or Drafting asking for outline approval before writing a full draft. The chat shows "waiting for you" with no fixed time limit — the user can take as long as they need. If a paused question is left unanswered for a very long time (on the order of a year), it eventually expires; the chat then shows "expired, start again" and the user restarts that task. In normal use, the user answers with a multiple-choice pick or free text well before that, and the agent resumes exactly where it paused.

![HITL question](./media/step-6-hitl.png)

> **[PLACEHOLDER]** Screenshot of a HITL question surface.

---

## Journey: Background jobs and the pipeline view (Partly built)

Today only Pipeline Manager runs are background jobs. The jobs routes exist, and `/pipeline?job=<id>` shows live step status. Without `?job=`, `/pipeline` still shows sample data. The 10-minute handoff for the other agents is planned.

For a request predicted to take longer than 10 minutes (or any multi-agent pipeline), the agent hands the work off to a background job. This choice is made once, before the agent starts answering — a request already streaming an answer never switches to a background job partway through. The user sees the job in a jobs list, and a dedicated `/pipeline` view for pipeline jobs shows live step status as the job progresses. The user is notified only inside the dashboard — there is no email notification.

![Jobs list](./media/step-7-jobs.png)

> **[PLACEHOLDER]** Screenshot of the jobs list / pipeline view.

---

## Journey: Export with review acknowledgment (Partly built)

The chat shows a list of the session's reports with download links (`GET /sessions/{sessionId}/exports`, `GET /exports/{exportId}/download`). The steps below are the target flow.

1. User selects "Export" on a completed answer.
2. User picks a format: PDF, DOCX, Markdown, CSV, or LaTeX.
3. User checks the box "I have reviewed this output." The Export button stays disabled until the box is checked. This acknowledgment is logged.
4. User downloads the file from a time-limited link.

![Export](./media/step-8-export.png)

> **[PLACEHOLDER]** Screenshot of the export dialog, once built.

---

## Journey: Settings and API keys (Partly built)

The Settings page has a **Model and mode defaults** section (F8, built). Per-user API keys still show "coming in a future update".

To set your defaults:

1. Open **Settings**.
2. In **Default model**, select a model, or keep **Use the system default**.
3. In **Default chat mode**, select **Science** or **Business**, or keep **No default**.
4. Select **Save**. The page shows "Saved."

Note: the app stores these defaults, but a new chat does not use them yet. A new chat uses the deployment default model unless you select a model in the chat.

Users can set their default model and mode, and can optionally add their own API key for a data source (for example NCBI) to get a higher rate limit, or configure additional paid sources. API keys are never shown back to the user once saved.

![Settings](./media/step-9-settings.png)

> **[PLACEHOLDER]** Screenshot of the settings page, once built.

---

## Planned journey: Delete a chat or the account (not built, deferred)

A user can delete a single chat (its history, trace, attachments, Knowledge Base chunks, and checkpoints) or delete their entire account (everything, including long-term memories and stored API keys). Deleting hides the item immediately in the dashboard; the underlying data is removed shortly after.

![Delete](./media/step-10-delete.png)

> **[PLACEHOLDER]** Screenshot of the delete confirmation, once built.

---

## Frequently Asked Questions (FAQ)

### Q: Is my data visible to other users?
**A:** No. Every user's chats, attachments, keys, and settings are fully isolated — there is no sharing and no teams (planned).

### Q: Can I use my own AI model provider?
**A:** No. All inference runs through Amazon Bedrock; you choose from the models the deployment allows.

### Q: How long is my chat history kept?
**A:** Indefinitely, until you delete the chat or your account.

---

## Getting Help

This section will list real support channels once the application is deployed.

---

## Next Steps

- See the [Deployment Guide](./deploymentGuide.md) for the deployment flow.
- See the [API Documentation](./APIDoc.md) for the route list and the status of each route.
- See the [Architecture Deep Dive](./architectureDeepDive.md) to understand how the system will work.
