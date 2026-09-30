# Architecture Deep Dive

CEI Life Sciences Bot — an AI-powered life sciences assistant.

---

## Architecture Diagram

![Architecture Diagram](./media/architecture.png)

---

## Target Architecture Flow

This section describes the planned production architecture. It does not describe resources that are already deployed. The current CDK stack defines only the AgentCore Code Interpreter. The repository does not yet contain the Session Runner, API Gateway, Fargate tasks, data stores, queues, authentication resources, or static hosting resources described below.

### 1. Client Access

User opens the web app. CloudFront serves the static Next.js build from S3. WAF protects the CDN. Amazon Cognito handles authentication.

### 2. Real-Time Chat (Streaming Path — SSE)

Authenticated user sends a message. The request goes through the HTTP API Gateway (streaming routes) to the **Session Runner** Lambda. The Session Runner passes the request to the **Session Router**, which delegates to the **AgentCore Runtime** (Amazon Bedrock AgentCore).

AgentCore runs the agent with access to its tool set (see [Agent Tools](#agent-tools)). This target path will stream text and status events to the client. Final CEI UI surface delivery by SSE is deferred. This path is planned for requests that complete within the **15-minute API Gateway streaming timeout**.

### 3. Background Jobs (Async Path)

For deep research or long-running tasks that exceed the 15-minute limit, the system starts a **Fargate task** running a copy of the agent. The Job ID Store (DynamoDB) tracks job status. A **Polling Lambda** lets the client check progress via REST routes on the same HTTP API Gateway.

The Fargate agent has the same tool access as the real-time path plus the ability to run longer workflows.

### 4. File Upload

User uploads a file. The **Attachment Upload Worker** Lambda creates a pre-signed S3 URL (max content size: 55MB). The file goes to the **Attachments Bucket**. An SQS message triggers the **Attachment Processor Function**, which extracts metadata, checks permissions, and writes to the **Attachment Metadata Store** (DynamoDB).

### 5. Session & User Management (REST Routes)

All served through the same HTTP API Gateway on REST routes:

- **Session Metadata Handler** — reads/writes session metadata (DynamoDB)
- **Session History Handler** — reads/writes chat history (DynamoDB). Will require lazy loading to prevent browser crashes on large conversation histories.
- **Fetch User Settings** — reads user preferences, configured AI providers, and metadata from the User Metadata Table (DynamoDB)

### 6. Knowledge Base

**Managed Knowledge Base Sync** keeps the **AgentCore Managed RAG** index up to date. AgentCore uses this for retrieval-augmented generation over life sciences documents.

---

## CEI UI Protocol

CEI UI 1.1 is the approved agent-to-UI protocol. A completed JSON response uses the `ui` field with content type `application/vnd.cei.ui+json`, protocol ID `cei-ui`. Servers emit version `1.1`; clients and stored chat history also accept `1.0`, since 1.1 only adds components (`Table`, `StatGrid`, `Callout`, `Timeline`, `Comparison`, `Artifact`) and never changes 1.0 meaning. The surface has `surface_id`, `revision`, `root_id`, and a flat `components` graph.

Models never emit component records or arbitrary UI. Deterministic presenters convert a typed `ResponseDocument` of presentation blocks into allowlisted components. The only model-authored code in the product is the `html` of an `Artifact` component, and it runs only inside a sandboxed iframe, never in the main page. Clients validate the full graph and show the plain-text response when validation or version checks fail. Interactive components use typed POST actions.

The implemented delivery phase supports completed JSON surfaces. The local Next.js CEI UI renderer can submit typed actions through preview proxy routes. These routes require `CEI_ENABLE_LOCAL_PROXY=true` and cannot be the production API because the production frontend target is a static S3 and CloudFront deployment.

Final production SSE surface delivery and progressive patches are deferred. The current repository has no production authentication or action endpoint, Session Runner, or implemented LangGraph pause and resume path. See [CEI UI Protocol 1.1](./cei-ui-protocol.md) for the contract, security limits, artifact sandboxing, and action states.

---

## Agent Tools

The AgentCore agent can call the following tools via MCP-based tool calling:

| Tool | Description |
|---|---|
| **Public DB Search Tools** | Search public life sciences databases |
| **Web Search** | General web search |
| **API Key Store** | Retrieve user-configured API keys for external services |
| **AgentCore Gateway** | Route requests between agent components |
| **Code Runner Sandbox** | Execute code in a sandboxed environment (tentative — under evaluation for safety and capability limits) |
| **Embedded UI Generator** | Generate UI components for rich responses |
| **Long & Short-term Memory** | Persist and recall context across sessions |
| **Observability Dashboard** | Monitor agent performance |
| **Life Sciences Operations** | Custom domain-specific tools (to be built) for life sciences workflows |

---

## Target Cloud Services / Technology Stack

### Frontend
- **Next.js 16** — React 19 with App Router, Tailwind CSS 4
- **S3 + CloudFront** — static hosting with CDN

### Authentication & Security
- **Amazon Cognito** — user authentication
- **AWS WAF** — CDN protection
- **CloudWatch** — dashboards, alarms, security notifications

### Compute
- **AWS Lambda** — all synchronous handlers (Session Runner, Polling, metadata handlers, attachment workers, user settings)
- **AWS Fargate** — long-running background agent tasks

### AI/ML
- **Amazon Bedrock AgentCore** — agent runtime with MCP-based tool calling
- **AgentCore Managed RAG** — retrieval-augmented generation over knowledge base

### Data Storage
- **Amazon DynamoDB** — Session Metadata Store, Chat History Store, Job ID Store, User Metadata Table, Attachment Metadata Store
- **Amazon S3** — Attachments Bucket (pre-signed upload, 55MB limit), Static Frontend Bucket
- **AWS Secrets Manager** — API Key Store (user-configured credentials for external services)

### Messaging
- **Amazon SQS** — attachment processing queue

### Networking
- **HTTP API Gateway** — single gateway serving both SSE streaming routes and REST routes

---

## Infrastructure as Code

AWS CDK (TypeScript). Single stack.

```
backend/
├── bin/
│   └── backend.ts          # CDK app entry point
├── lib/
│   └── backend-stack.ts    # Main stack definition
├── lambda/                 # Lambda function handlers (to be created)
└── test/
    └── backend.test.ts     # CDK stack tests
```

cdk-nag AwsSolutionsChecks is scoped to the stack (not the app entry point). Suppressions require ADR-format reason strings.

---

## Design Decisions

### Code Execution: AgentCore Code Interpreter

AgentCore provides a built-in **Code Interpreter** — containerized sandbox supporting Python, JS, and TS. Each execution runs in an isolated container. Network access is configurable (isolated or controlled public). Timeout: 15 min default, up to 8 hours. File uploads: 100 MB inline, 5 GB via S3.

For custom life sciences tools, use **AgentCore Gateway** (governed, with auth and policy enforcement) or register as **MCP server endpoints**. Start with network-isolated sandbox mode and open specific endpoints as needed.

### Custom Tool Integration Strategy

| Tool type | Approach |
|---|---|
| Life Sciences Operations | AgentCore Gateway or MCP servers — governed access to domain APIs |
| Code sandbox (data analysis, transformations) | AgentCore Code Interpreter |
| Web search, public DB search | Built-in AgentCore tools |
| Fallback for anything too restrictive | Inline functions (pause agent, run Lambda/Fargate, return result) |

---

## Open Questions

1. **Chat History retention** — What is the memory retention policy? The diagram notes that messages may age out. Need to define TTL or archival strategy.
2. **Lazy loading for Chat History** — Required to prevent browser crashes on large histories. Need to define pagination strategy.
