![ASU AI CIC Logo](./media/project-closure/ASU-AI-CIC-logo.png)

**Project Completion Documentation** 

CEI Life Sciences Bot

> **TO BE SUPPLIED:** client logo, saved as `./media/project-closure/client-logo.png`.

![Powered by AWS](./media/project-closure/powered-by-aws.png)

**Authors:** 

**Aaditya Jindal, Sreeram Sravana Prasad, Sandrah Bosaire**

**Project Closure Date:**

**19-November-26**

### 

**Table of Contents**

**1[. Executive Summary](#1-executive-summary)**

**2[. Project Overview](#2-project-overview)**

2[.1 AWS Leads](#21-aws-leads)

2[.2 ASU Build Team](#22-asu-build-team)

2[.3 Project Timeline](#23-project-timeline)

2[.4 Project Timeline and Sprints](#24-project-timeline-and-sprints)

[**3\. Project Performance**](#3-project-performance)

[3.1 Problem Statement](#31-problem-statement)

[3.2 Project Scope](#32-project-scope)

[3.3 Deliverables](#33-deliverables)

[**4\. Development**](#4-development)

[4.1 UI/UX](#41-uiux)

[4.2 System Architecture](#42-system-architecture)

[4.3 Technology Stack](#43-technology-stack)

[4.4 Key Features Implementation](#44-key-features-implementation)

[4.5 Deployment](#45-deployment)

[**5\. Challenges**](#5-challenges)

[**6\. Future Scope**](#6-future-scope)

[**7\. Appendix**](#7-appendix)

### 

### **1\. Executive Summary** {#1-executive-summary}

### ---

CEI Life Sciences Bot is an AI assistant for life sciences founders and early-stage researchers. These users must do literature and clinical research, write grant applications and investor material, and understand their competitive, market, and legal landscape, often with no dedicated research or business staff.

The ASU AI Cloud Innovation Center (CIC) built a multi-user, serverless solution on AWS. Users sign up with their own account and work in a web dashboard. Four specialist AI agents run on Amazon Bedrock AgentCore:

- **Research Agent** – deep research with citation grounding across 12 public data sources (PubMed, ClinicalTrials.gov, ChEMBL, PubChem, Open Targets, openFDA, PatentsView, bioRxiv, medRxiv, ICD-10, NPI, CMS Coverage) plus NCBI tools.
- **Drafting Agent** – grant, VC, journal, and whitepaper drafts with an outline approval step and export to PDF, DOCX, Markdown, CSV, LaTeX, and TXT.
- **Analysis Agent** – competitor, market, and legal landscape analysis, each with a fixed framework.
- **Pipeline Manager** – runs the other agents in sequence or at the same time, after the user approves the plan, as a background job with live step status.

Answers stream to the browser as they are built, with a "thinking block" that shows tool calls and sources. Agents can stop and ask the user a question (human-in-the-loop). Users can pick a Bedrock model per chat and a science or business mode, and attach files that the agents retrieve through a Knowledge Base. A typed UI protocol (CEI UI) makes sure that models never write UI code, which keeps the dashboard safe. Each user's data is isolated by their Cognito identity.

The result gives founders fast, cited research and first drafts in minutes instead of days, and gives CIC a reusable reference architecture for multi-agent applications on AgentCore. The code and CDK definitions are complete for the MVP scope; several components still need live verification on AWS (see [Future Scope](#6-future-scope)).

### 

### **2\. Project Overview**

---

 {#2-project-overview}

#### **2.1 AWS Leads** {#21-aws-leads}

> **TO BE SUPPLIED:** the AWS team members and their roles (for example Solution Architect, Digital Innovation Lead, Project Manager, Account Manager).

#### **2.2 ASU Build Team** {#22-asu-build-team}

- Aaditya Jindal – Cloud Developer
- Sreeram Sravana Prasad – Cloud Developer
- Sandrah Bosaire – UI/UX Developer

#### **2.3 Project Timeline** {#23-project-timeline}

27-August-26 - 19-November-26

#### **2.4 Project Timeline and Sprints** {#24-project-timeline-and-sprints}

The dates come from the repository history. The phase IDs (S1–S5, F1–F21) are defined in `.agents/roadmap.md`.

| Item # | Iterations | Deliverables | Date planned |
| :---- | :---- | :---- | :---- |
| **1** | **Pre POC** | Repository set up; requirements and target architecture; agent architecture decisions | **27-Aug-26 – 13-Sep-26** |
| **2** | **Sprint 1** | Research agent (LangGraph) with data source tools, Jinja2 prompts, and Code Interpreter; PDF/DOCX/LaTeX export tools; Drafting agent with research and charts | **14-Sep-26 – 27-Sep-26** |
| **3** | **Sprint 2** | CEI UI response schema and preview renderer; build plan docs; guided deploy script and CodeBuild-only build (F1); Cognito and S3/CloudFront/WAF hosting (F2) | **28-Sep-26 – 04-Oct-26** |
| **4** | **Sprint 3 (client demo MVP)** | REST API and session store (F3); tool scope spike (S2); openFDA and PatentsView sources; Analysis agent (3 types) and Pipeline Manager MVP; Vite dashboard Task Launcher and `/pipeline` view | **05-Oct-26 – 11-Oct-26** |
| **5** | **Sprint 4** | AgentCore Gateway with Cedar policy and data source targets (F4); Session Router and Runner with `/chat/stream` (F5); CEI UI 1.2 streaming and thinking block (F6) | **02-Nov-26 – 08-Nov-26** |
| **6** | **Sprint 5** | Human-in-the-loop (F13); model picker, blocklist, Guardrails, prompt caching (F8); free-text router (F9); web search and Life Sciences Operations (F12); long-term memory (F11); attachments with managed Knowledge Base (F10); background jobs (F16); account delete behind a flag (F19); alarms (F20) | **09-Nov-26 – 15-Nov-26** |
| **7** | **Sprint 6 (closure)** | Textual TUI deploy tool; founder onboarding with 18+ age gate; deploy fixes; project documentation | **16-Nov-26 – 19-Nov-26** |

### 

### **3\. Project Performance** {#3-project-performance}

### ---

### 

#### **3.1 Problem Statement** {#31-problem-statement}

Life sciences founders spend a large part of their time on work that is not science: they search many separate databases (literature, trials, compounds, targets, patents, regulatory data), they write grants and investor documents, and they track competitors, markets, and the legal landscape.

- **Pain points:** each data source has its own search interface and data format; results must be cross-checked and cited by hand; drafting a grant or a VC document from that research takes days.
- **Impact:** small teams lose time that they could use for research and fundraising, and they can miss important evidence or competitors.
- **Why existing tools were not sufficient:** general chat assistants do not ground their answers in life sciences databases, cannot show reliable citations, and can invent facts. Single-database tools do not combine sources, draft documents, or run multi-step work.
- **Need:** a secure, multi-user assistant that searches trusted life sciences sources, cites every claim, drafts and exports documents, analyzes the landscape, and runs longer multi-step pipelines, with each user's data kept private.

#### **3.2 Project Scope** {#32-project-scope}

##### **In Scope**

- Vite + React dashboard on S3, CloudFront, and WAF, with Cognito self sign-up, email-domain allowlist, and an 18+ onboarding gate
- One Regional REST API with a Cognito authorizer, throttling, and SSE streaming on `/chat/stream`
- Session Runner and Session Router (dashboard task lookup and free-text classifier)
- Research, Drafting, Analysis (competitor, market, legal landscape), and Pipeline Manager agents on AgentCore Runtime
- AgentCore Gateway with Cedar policy and per-source token scopes for 12 data sources and NCBI tools
- Life Sciences Operations Lambda (sequence, protein, primer, chemistry, and lab calculations)
- CEI UI 1.1 completed responses and CEI UI 1.2 progressive streaming with a thinking block
- Human-in-the-loop questions, outline approval, scope confirmation, and plan approval
- Per-chat model picker with a global blocklist, Bedrock Guardrails, and prompt caching
- Chat attachments through a Bedrock Knowledge Base on S3 Vectors
- Document export (PDF, DOCX, Markdown, CSV, LaTeX, TXT)
- Background jobs for pipelines, with a watchdog and live step status
- Chat history store, session metadata, and user settings in DynamoDB
- CloudWatch dashboard, alarms, and SNS admin email alerts
- Guided deploy tool (`deploy.sh`) that deploys through CodeBuild

##### **Out of Scope**

- Programmatic tool calling through the Code Interpreter (deferred, needs a safety review)
- Slides (PPTX) export
- Paid, user-configured data sources
- A UI to see or edit long-term memories
- Delete of a single chat (only full account delete is in scope)
- Teams, sharing, or multi-tenant organizations
- MFA
- Separate dev, staging, and production environments
- Email notifications for finished jobs

#### **3.3 Deliverables** {#33-deliverables}

* Functional web dashboard (`frontend/`)
* AWS CDK infrastructure: `BackendStack` and `EdgeWafStack` (`backend/`)
* Four specialist agents and a single-source Tool Agent graph (`backend/agents/`)
* Data source, NCBI, Life Sciences Operations, and platform Lambda functions (`backend/lambdas/`)
* Guided deploy tool and CodeBuild build specs (`deploy.sh`, `deploy/`, `buildspec.yml`)
* CEI UI protocol specification (`docs/cei-ui-protocol.md`)
* API documentation (`docs/APIDoc.md`)
* Architecture deep dive, deployment guide, user guide, modification guide, operations and observability guides (`docs/`)
* Decision log, roadmap, specs, and phase files (`.agents/`)
* Automated tests: Jest (CDK), pytest (agents, Lambdas, deploy tool), and Vitest (frontend)
* Source code and configuration files

### 

### **4\. Development** {#4-development}

---

#### 

#### **4.1 UI/UX** {#41-uiux}

#### ---

#### 

![UI/UX Design Process](./media/project-closure/UI-UX-design-process.png)

#### **Who are the users of this application?**

The users are founders and small teams of early-stage life sciences and biotech companies, and the researchers who work with them. They have strong science knowledge but limited time, and they often do not have dedicated research analysts, grant writers, or business analysts. Each user works alone in their own account; there is no sharing between users.

#### **Why are the users using this application?**

Users want to:

- Find and combine evidence from literature, clinical trials, compounds, targets, patents, and regulatory data, with citations they can check.
- Produce first drafts of grants, investor documents, journal articles, and whitepapers, and export them.
- Understand their competitors, market, and legal landscape.
- Run a longer, multi-step task (for example research, then analysis, then a draft) without watching each step.

#### **What is the customer opportunity statement?**

How might we give life sciences founders a trusted, cited research and drafting partner that combines many specialist data sources in one place, so that they can spend less time on searching and writing and more time on their science and their company?

#### **User Interface**

![Task Launcher](./media/screenshot1.png)
> *Image to be added (`./media/screenshot1.png`):* the Task Launcher, where the user starts a Research, Drafting, Analysis, or Pipeline task.

![Streamed answer with thinking block](./media/screenshot2.png)
> *Image to be added (`./media/screenshot2.png`):* a Research answer while it streams, with the thinking block open and cited sources.

#### 

#### **4.2 System Architecture** {#42-system-architecture}

The solution is serverless and runs in one AWS account and one Region (plus `us-east-1` for the CloudFront WAF). The browser loads a static dashboard from S3 through CloudFront and WAF and signs in with Cognito. All calls go to one Regional REST API with a Cognito authorizer. The chat route streams Server-Sent Events from a Node.js Session Runner Lambda. A Python Session Router picks the agent and gets short-lived, scoped machine tokens. The agents run on Amazon Bedrock AgentCore Runtime, call Bedrock models through Guardrails, and reach data through an AgentCore Gateway where Cedar policies allow only the sources in each token's scope. DynamoDB holds sessions, chat history, jobs, and settings; a Bedrock Knowledge Base on S3 Vectors holds attachments; CloudWatch and SNS handle monitoring and alerts.

##### **Architecture Diagram**

![Architecture Diagram](./media/architecture.png)

##### **Workflow Description**

1. **Step 1:** The user signs in through Cognito managed login (PKCE) and completes onboarding. The dashboard loads its runtime configuration from `/config.json`.
2. **Step 2:** The user starts a task from the Task Launcher or types a free-text message. The dashboard sends the turn to `POST /chat/stream` with the Cognito access token.
3. **Step 3:** The Session Runner checks the request and calls the Session Router. The Router uses a fixed lookup for a dashboard task, or a small classifier model for free text (below 0.7 confidence it asks the user to pick an agent). It gets a scoped Gateway token and calls the selected AgentCore runtime.
4. **Step 4:** The agent plans its work and calls tools through the Gateway (data sources, NCBI, Life Sciences Operations, web search, Knowledge Base) and the Code Interpreter. If it needs the user, it pauses with a human-in-the-loop question and resumes after a typed answer to `/ui-actions`.
5. **Step 5:** Deterministic presenters turn the result into a CEI UI component graph. The Runner streams progressive patches and the completed surface to the browser, and stores the turn and trace in the Chat History Store. Pipeline runs continue as background jobs, and `/pipeline?job=<id>` shows live step status.

#### 

#### **4.3 Technology Stack** {#43-technology-stack}

##### **Frontend**
- **Vite + React 19 + Tailwind CSS 4 + react-router**: single-page dashboard with a CEI UI renderer, an SSE stream reducer, and `oidc-client-ts` for Cognito sign-in
- **Hosting**: Amazon S3 + Amazon CloudFront + AWS WAF

##### **Backend Services**
- **Amazon API Gateway (REST API v1, Regional)**: one API; `STREAM` mode on `/chat/stream`, `BUFFERED` on every other route; one Cognito authorizer
- **AWS Lambda**: Session Runner (Node.js), Session Router, session metadata and history, jobs and watchdog, exports, UI actions, models and settings, profile, attachments, account delete, data source targets, NCBI, and Life Sciences Operations (Python)
- **Amazon Cognito**: self sign-up user pool, SPA PKCE client, pre-sign-up domain check, pre-token-generation trigger, and the Router machine-to-machine client with per-source scopes
- **Amazon SQS**: attachment processing queue with a dead-letter queue

##### **Data Storage**
- **Amazon DynamoDB**: Session Metadata, Chat History, Job ID, and User Metadata stores, all partitioned by Cognito `sub`
- **Amazon S3**: dashboard assets, attachments bucket (pre-signed upload, 55 MB maximum), exports, and S3 Vectors for the Knowledge Base

##### **AI/ML Services**
- **Amazon Bedrock and Amazon Bedrock AgentCore**: Bedrock foundation models (user-selected per chat, with a global blocklist and prompt caching), Bedrock Guardrails on every model call, AgentCore Runtime for the LangGraph agents, AgentCore Gateway with Cedar policy, AgentCore Code Interpreter, AgentCore Memory, the AgentCore Web Search connector, and a Bedrock Knowledge Base

##### **Infrastructure**
- **AWS CDK**: Infrastructure as Code for deployment (TypeScript, with cdk-nag AwsSolutions checks)
- **AWS CodeBuild**: runs every build and deploy step; `deploy.sh` starts it from a guided terminal UI
- **Amazon CloudWatch + Amazon SNS**: dashboard, alarms, and admin email alerts
- **AWS Secrets Manager**: GitHub token for the deploy build, and the planned per-user API key store

#### 

#### **4.4 Key Features Implementation** {#44-key-features-implementation}

##### **Feature 1: Cited multi-source research**
The Research agent is a LangGraph graph that plans the research, selects sources, and calls Gateway tools for each source. Each data source is its own Lambda target with a fixed tool schema. Cedar policies in the Gateway allow a call only when the token has the scope for that source. A grounding step checks citations; tables, charts, and statistics show a "verifying citations" state until grounding finishes.

##### **Feature 2: Streaming answers with CEI UI**
Agents never emit UI directly. They return a typed `ResponseDocument`, and deterministic presenters build an allowlisted CEI UI component graph from it. CEI UI 1.2 streams this graph as progressive patch events over SSE, through the Session Runner, with a heartbeat every 17 seconds. The dashboard renders only known components, and validators on the backend and frontend reject anything outside the allowlist. Model-authored Artifact HTML runs only inside a sandboxed iframe.

##### **Feature 3: Human-in-the-loop and pipelines**
Agents pause with a LangGraph `interrupt` to ask a clarifying question, to get outline approval (Drafting), to confirm scope (Analysis), or to approve a plan (Pipeline Manager). The dashboard shows the question as a CEI UI surface, and a typed POST to `/ui-actions` resumes the run from its checkpoint. The Pipeline Manager runs approved plans as background jobs, runs steps in sequence or at the same time, and reports live step status through the Job ID Store.

##### **Feature 4: Per-chat model, mode, and attachments**
Each chat has a science or business mode and a Bedrock model that the user selects before the first message (blocked models are not shown). Prompt caching is used where the model supports it. Attachments upload through a pre-signed POST, go through an SQS processor into the Knowledge Base, and are retrieved with a metadata filter so that each chat sees only its own files.

#### 

#### **4.5 Deployment** {#45-deployment}

All build and deploy steps run in AWS CodeBuild, not on the local computer. The `deploy.sh` wrapper starts a Textual terminal UI that collects the configuration, writes `backend/cei.deploy.json`, creates or updates the CodeBuild project and its IAM role, and starts a build. The build builds the frontend and runs `cdk bootstrap` and `cdk deploy` for both stacks. Full detail is in the [Deployment Guide](./deploymentGuide.md).

##### **Prerequisites**
- AWS credentials for the target account (CloudShell works with no extra setup), with the permissions listed in the Deployment Guide
- Python 3.9 or later with `pip`, bash, and git (git is optional)
- Access to the selected Amazon Bedrock models in the deploy Region
- A GitHub personal access token, only if the repository is private

##### **Deployment Steps**
1. Clone the repository and run `./deploy.sh` from the repository root (use `./deploy.sh --dry-run` first to check the configuration with no AWS writes).
2. In the guided screens, select the Region, the admin alert email, the allowed sign-up email domains, the models, and the repository and branch; then select **Deploy**.
3. Watch the live CodeBuild log until the build ends, then open the `SiteUrl` stack output, sign up with an allowed email domain, and complete onboarding.
4. For later updates, run `./deploy.sh --yes` to redeploy the saved configuration.

### 

### **5\. Challenges** {#5-challenges}

#### ---

- **Challenge 1:** The first design used an API Gateway HTTP API, but HTTP APIs cannot stream responses.
  - **Solution:** The team moved every route to one Regional REST API (v1), with `responseTransferMode: STREAM` on `/chat/stream` and a Node.js Session Runner Lambda with native response streaming.

- **Challenge 2:** AgentCore Runtime does not document CORS support, and its JWT authorizer rejects browser preflight requests.
  - **Solution:** An `AgentProxy` Lambda function URL answers CORS, checks the bearer token and session ID, and forwards the caller's own token to the runtime, where the Cognito authorizer verifies it.

- **Challenge 3:** Models that generate UI directly can produce unsafe or broken output.
  - **Solution:** The CEI UI protocol: models produce only typed presentation blocks, deterministic presenters build an allowlisted component graph, and validators on both sides enforce it. Model-authored HTML is only allowed in a sandboxed iframe.

- **Challenge 4:** Long multi-agent pipelines can run longer than a normal request, and the first plan used a separate Fargate cluster.
  - **Solution:** AgentCore Runtime async sessions (up to 8 hours) replaced Fargate, with a Job ID Store, a watchdog that resumes stale jobs from checkpoint, and live step status in `/pipeline`.

- **Challenge 5:** The CloudFront WAF web ACL must be in `us-east-1`, but the application can deploy to any Region.
  - **Solution:** A separate `EdgeWafStack` always deploys to `us-east-1`, and `BackendStack` uses it through `crossRegionReferences`.

- **Challenge 6:** Each agent must reach only the data sources it is allowed to use.
  - **Solution:** Cognito issues short-lived machine tokens with one scope per source, held only by the Session Router, and AgentCore Gateway Cedar policies are default-deny with one permit per source target.

- **Challenge 7:** The full CDK Jest suite took 20–70 minutes because each test synthesized the stack again.
  - **Solution:** A per-file synth cache (`backend/test/helpers/memo-synth.ts`) cut the full run to about 18 minutes.

### 

### **6\. Future Scope** {#6-future-scope}

### ---

* Live verification on AWS: run and record the deployed checks in each phase file, switch the Gateway policy from `LOG_ONLY` to `ENFORCE`, and complete the open spikes (S1 streaming, S3 async, S4 HITL, S5 RAG).
* Tool agents: deploy the single-source Tool Agent runtime with its 11 profiles and the `/tools/{source}/chat/stream` route.
* User API keys: the settings page and Secrets Manager store for per-user source keys (for example NCBI), for higher rate limits.
* Attachments: per-file view and delete, image input to vision models, CSV files in the Code Interpreter, and attachments in the account delete sweep.
* Streaming and background jobs for every agent: CEI UI 1.2 streams for Analysis and Pipeline, and the 10-minute handoff to a background job for Research, Drafting, and Analysis.
* Durable human-in-the-loop checkpoints for all agents, not only Research.
* Long-term memory for Drafting, Analysis, and Pipeline (writes, not only recall), and a UI to see and edit memories.
* Slides (PPTX) export and an ad hoc export route.
* Paid, user-configured data sources.
* Programmatic tool calling through the Code Interpreter, after a safety review.
* Hardening: a full security review, a load test, and proven alarm delivery.

#### 

### **7\. Appendix** {#7-appendix}

#### ---

**GitHub Repository** - [https://github.com/ASUCICREPO/CEI_Life_Sciences_Bot](https://github.com/ASUCICREPO/CEI_Life_Sciences_Bot)

**Figma Link (Internal)** - For the currently implemented version - **TO BE SUPPLIED**

**Figma Link (Internal)** - For the future scope - **TO BE SUPPLIED**

**Project Demo Recording** - **TO BE SUPPLIED**

**Drive Link** - **TO BE SUPPLIED**

**Additional resources:**

**[Architecture Deep Dive](./architectureDeepDive.md)**

**[Deployment Guide](./deploymentGuide.md)**

**[User Guide](./userGuide.md)**

**[API Documentation](./APIDoc.md)**

**[CEI UI Protocol](./cei-ui-protocol.md)**
