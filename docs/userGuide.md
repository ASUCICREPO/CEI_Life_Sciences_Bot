# User Guide

Status: **Planned — not built.** This page outlines the planned user journeys for the CEI Life Sciences Bot. It is not a walkthrough of a deployed application, because there is no deployed frontend, authentication, or API yet (see [Provisioned Resources](./provisioned-resources.md)). This guide will fill in with real screenshots and step-by-step instructions once each journey exists. Full detail: `.agents/specs/architecture.md`, `.agents/specs/api.md`, `.agents/decision-log.md`.

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

## Planned journey: Sign up

1. User opens the application URL and selects "Sign up."
2. User enters an email address and password. Cognito enforces an email-domain allowlist set by the deployment; sign-up from a disallowed domain is rejected before verification.
3. User verifies their email address.
4. No MFA step exists in the planned design.

![Sign up](./media/step-1-signup.png)

> **[PLACEHOLDER]** Screenshot of the sign-up screen, once built.

---

## Planned journey: Start a chat

A user can start a chat in two ways:

- **Pick a task** from a task launcher (a specific research, drafting, or analysis task). This routes deterministically to the matching specialist agent — no model call decides the routing.
- **Type free text.** A small, fast classifier model picks the best-fit specialist agent (Research, Drafting, Analysis, or Pipeline Manager) automatically. If the system is not sure which agent fits, it asks the user to pick one from a short list of the four agents instead of guessing.

![Start a chat](./media/step-2-start-chat.png)

> **[PLACEHOLDER]** Screenshot of the chat start screen, once built.

---

## Planned journey: Per-chat mode and model

Each chat has a **mode** (science or business) chosen when the chat starts, changeable at any point in that chat. Each chat also has a **model picker** — the user selects any available Bedrock model, from a list that already excludes models the deployment has blocked.

![Mode and model picker](./media/step-3-mode-model.png)

> **[PLACEHOLDER]** Screenshot of the mode/model controls, once built.

---

## Planned journey: Watching an answer build (thinking block)

While the agent works, the user sees:
- A live step status (for example "Searching PubMed").
- A collapsible **thinking block** showing tool calls, sources accessed, and the model's reasoning text, expanding automatically while the agent works and collapsing once the answer is ready. The user can scroll up and re-expand it later.
- A live token/cost counter.
- The answer itself streaming in, piece by piece, with a "verifying citations" state on tables/charts/statistics until grounding finishes.

![Thinking block](./media/step-4-thinking-block.png)

> **[PLACEHOLDER]** Screenshot of an in-progress answer with the thinking block expanded, once built.

---

## Planned journey: Attachments

1. User uploads a file (PDF, DOCX, TXT, MD, CSV, PNG, or JPG; up to 55 MB) into a specific chat.
2. The file becomes available only within that chat.
3. The agent retrieves relevant chunks from the file automatically as part of its research, rather than reading the whole file into context. CSV files are also available to the Code Interpreter; images are also available directly to models that support vision.

![Attachments](./media/step-5-attachments.png)

> **[PLACEHOLDER]** Screenshot of the attachment upload UI, once built.

---

## Planned journey: Answering agent questions (HITL)

Some agents pause mid-task to ask the user a question — for example, Research asking for clarification when no data source clearly fits, or Drafting asking for outline approval before writing a full draft. The chat shows "waiting for you" with no fixed time limit — the user can take as long as they need. If a paused question is left unanswered for a very long time (on the order of a year), it eventually expires; the chat then shows "expired, start again" and the user restarts that task. In normal use, the user answers with a multiple-choice pick or free text well before that, and the agent resumes exactly where it paused.

![HITL question](./media/step-6-hitl.png)

> **[PLACEHOLDER]** Screenshot of a HITL question surface, once built.

---

## Planned journey: Background jobs and the pipeline view

For a request predicted to take longer than 10 minutes (or any multi-agent pipeline), the agent hands the work off to a background job. This choice is made once, before the agent starts answering — a request already streaming an answer never switches to a background job partway through. The user sees the job in a jobs list, and a dedicated `/pipeline` view for pipeline jobs shows live step status as the job progresses. The user is notified only inside the dashboard — there is no email notification.

![Jobs list](./media/step-7-jobs.png)

> **[PLACEHOLDER]** Screenshot of the jobs list / pipeline view, once built.

---

## Planned journey: Export with review acknowledgment

1. User selects "Export" on a completed answer.
2. User picks a format: PDF, DOCX, Markdown, CSV, or LaTeX.
3. User checks the box "I have reviewed this output." The Export button stays disabled until the box is checked. This acknowledgment is logged.
4. User downloads the file from a time-limited link.

![Export](./media/step-8-export.png)

> **[PLACEHOLDER]** Screenshot of the export dialog, once built.

---

## Planned journey: Settings and API keys

Users can set their default model and mode, and can optionally add their own API key for a data source (for example NCBI) to get a higher rate limit, or configure additional paid sources. API keys are never shown back to the user once saved.

![Settings](./media/step-9-settings.png)

> **[PLACEHOLDER]** Screenshot of the settings page, once built.

---

## Planned journey: Delete a chat or the account

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

- See the [Deployment Guide](./deploymentGuide.md) for the planned deployment flow.
- See the [API Documentation](./APIDoc.md) for the planned route list.
- See the [Architecture Deep Dive](./architectureDeepDive.md) to understand how the system will work.
