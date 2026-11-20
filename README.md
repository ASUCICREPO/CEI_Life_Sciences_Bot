# CEI Life Sciences Bot

CEI Life Sciences Bot is an AI assistant for life sciences founders and researchers. It does deep research with citations across 12 public data sources plus NCBI tools, drafts documents (grants, VC decks, journal articles, whitepapers), runs competitor, market, and legal landscape analysis, and chains these tasks into multi-step pipelines. Each user signs up with their own account, and all their chats, settings, and attachments stay isolated from every other user.

---

## Disclaimers

Customers are responsible for making their own independent assessment of the information in this document. This document:

(a) is for informational purposes only,

(b) references AWS product offerings and practices, which are subject to change without notice,

(c) does not create any commitments or assurances from AWS and its affiliates, suppliers or licensors. AWS products or services are provided "as is" without warranties, representations, or conditions of any kind, whether express or implied. The responsibilities and liabilities of AWS to its customers are controlled by AWS agreements, and this document is not part of, nor does it modify, any agreement between AWS and its customers, and

(d) is not to be considered a recommendation or viewpoint of AWS.

Additionally, you are solely responsible for testing, security and optimizing all code and assets on GitHub repo, and all such code and assets should be considered:

(a) as-is and without warranties or representations of any kind,

(b) not suitable for production environments, or on production or other critical data, and

(c) to include shortcuts in order to support rapid prototyping such as, but not limited to, relaxed authentication and authorization and a lack of strict adherence to security best practices.

All work produced is open source. More information can be found in the GitHub repo.

---

## Visual Demo

![User Interface Demo](./docs/media/user-interface.gif)

> *Image to be added: `docs/media/user-interface.gif`, a short recording of the dashboard (sign-in, Task Launcher, a streamed Research answer).*

---

## Table of Contents

| Index                                               | Description                                              |
| :-------------------------------------------------- | :------------------------------------------------------- |
| [High Level Architecture](#high-level-architecture) | High level overview illustrating component interactions  |
| [Deployment Guide](#deployment-guide)               | How to deploy the project                                |
| [User Guide](#user-guide)                           | End-user instructions and walkthrough                    |
| [API Documentation](#api-documentation)             | Documentation on the APIs the project uses               |
| [Directories](#directories)                         | General project directory structure                      |
| [Modification Guide](#modification-guide)           | Guide for developers extending the project               |
| [Troubleshooting](#troubleshooting)                 | Common issues and solutions                              |
| [Credits](#credits)                                 | Contributors and acknowledgments                         |
| [License](#license)                                 | License information                                      |

---

## High Level Architecture

Users open a Vite + React single-page dashboard served from Amazon S3 through Amazon CloudFront and AWS WAF, and sign in with Amazon Cognito (self sign-up, email verification, an email-domain allowlist, and an 18+ onboarding gate). The dashboard calls one Regional Amazon API Gateway REST API, protected by a Cognito authorizer. Chat turns go to `/chat/stream`, where a Node.js Session Runner Lambda streams Server-Sent Events back to the browser. The Session Runner calls a Python Session Router Lambda, which picks the right agent: a fixed lookup for dashboard tasks, or a small classifier model for free text. The Router then calls one of four LangGraph agents on Amazon Bedrock AgentCore Runtime: Research, Drafting, Analysis, or Pipeline Manager.

The agents call Amazon Bedrock models (the user picks the model per chat; Amazon Bedrock Guardrails apply to every call). They reach their tools through an AgentCore Gateway, where Cedar policies allow each agent only the data sources in its token scope. Gateway targets are Lambda functions for 12 data sources (PubMed, ClinicalTrials.gov, ChEMBL, PubChem, Open Targets, openFDA, PatentsView, bioRxiv, medRxiv, ICD-10, NPI, CMS Coverage), NCBI tools, a deterministic Life Sciences Operations Lambda (Biopython/RDKit), and the AgentCore Web Search connector (off by default). Agents also use the AgentCore Code Interpreter, AgentCore Memory, and a Bedrock Knowledge Base over S3 Vectors for chat attachments. Amazon DynamoDB stores session metadata, chat history, jobs, and user settings. Amazon CloudWatch and Amazon SNS give dashboards, alarms, and admin email alerts.

Every agent answer uses the CEI UI protocol: deterministic presenters turn the agent output into an allowlisted component graph, and the browser renders only those components. Models never write UI code, except sandboxed Artifact HTML inside an iframe.

![Architecture Diagram](./docs/media/architecture.png)

For a detailed explanation of the architecture and architectural decisions, see the [Architecture Deep Dive](./docs/architectureDeepDive.md).

---

## Deployment Guide

For complete deployment instructions, see the [Deployment Guide](./docs/deploymentGuide.md).

**Quick Start:**
1. Get AWS credentials for the target account (CloudShell works with no setup), plus Python 3.9 or later, bash, and git.
2. From the repository root, run `./deploy.sh`. The guided terminal UI asks for the Region, the admin alert email, the allowed sign-up email domains, the Bedrock models, and the GitHub repository and branch. Use `./deploy.sh --dry-run` first to check the configuration with no AWS writes.
3. Select **Deploy**. The tool creates a CodeBuild project that builds the frontend and runs `cdk bootstrap` and `cdk deploy`. When the build ends, open the `SiteUrl` stack output, sign up with an allowed email domain, and complete onboarding.

---

## User Guide

For detailed usage instructions with screenshots, see the [User Guide](./docs/userGuide.md).

---

## API Documentation

For complete API reference, see the [API Documentation](./docs/APIDoc.md).

---

## Modification Guide

For developers looking to extend or modify this project, see the [Modification Guide](./docs/modificationGuide.md).

---

## Directories

```
├── backend/
│   ├── bin/
│   │   └── backend.ts                 # CDK app entry point
│   ├── lib/
│   │   ├── backend-stack.ts           # BackendStack (API, Cognito, hosting, agents, data)
│   │   ├── edge-waf-stack.ts          # EdgeWafStack (CloudFront WAF, us-east-1)
│   │   ├── config/                    # Deploy config loading
│   │   └── constructs/                # Gateway, Cedar policy, runtimes, jobs, RAG, observability
│   ├── lambdas/
│   │   ├── session-runner/            # Node.js SSE streaming for /chat/stream
│   │   ├── session-router/            # Agent routing and free-text classifier
│   │   ├── session-metadata-handler/  # /sessions routes
│   │   ├── session-history-handler/   # /sessions/{id}/history
│   │   ├── data-source-*/             # 12 data-source Gateway targets
│   │   ├── ncbi/, lsops/              # NCBI tools, Life Sciences Operations
│   │   ├── jobs-handler/, watchdog/   # Background jobs
│   │   ├── attachment-presign/, attachment-processor/
│   │   ├── exports-handler/, ui-actions/, models-settings-handler/, profile-handler/
│   │   ├── account-delete/, delete-worker/
│   │   ├── pre-signup-domain-check/, pre-token-generation/, token-refresh/
│   │   ├── agent-proxy/, client-diagnostics/
│   │   └── layers/, shared/
│   ├── agents/
│   │   ├── research/, drafting/, analysis/, pipeline_manager/, tool_agent/
│   │   ├── shared/                    # Schemas, presenters, prompts, streaming, memory, tools
│   │   ├── *_main.py                  # AgentCore runtime entry points
│   │   └── tests/
│   ├── test/                          # Jest CDK tests
│   ├── cdk.json
│   └── package.json
├── frontend/
│   ├── src/
│   │   ├── pages/                     # Home, Task Launcher, Chat, Research, Pipeline, History, Settings
│   │   ├── components/                # Chat, CEI UI renderer, streaming, shell, tour
│   │   ├── lib/                       # API clients, auth, CEI UI schema, stream reducer
│   │   └── main.tsx
│   ├── public/                        # Static assets, config.example.json
│   ├── index.html
│   ├── vite.config.ts
│   └── package.json
├── deploy/
│   ├── cei_deploy/                    # Textual TUI deploy tool
│   └── tests/
├── archive/
│   └── frontend-next/                 # Old Next.js test harness (not deployed)
├── docs/
│   ├── architectureDeepDive.md
│   ├── deploymentGuide.md
│   ├── userGuide.md
│   ├── APIDoc.md
│   ├── modificationGuide.md
│   ├── cei-ui-protocol.md
│   ├── operations.md, observability.md, provisioned-resources.md
│   ├── projectClosure.md
│   └── media/
├── .agents/                           # Decision log, roadmap, specs, phase files
├── buildspec.yml                      # CodeBuild deploy build
├── deploy.sh                          # Guided deploy wrapper
├── LICENSE
└── README.md
```

### Directory Explanations:

1. **backend/** - AWS CDK infrastructure (TypeScript), Lambda functions, and agent code
   - `bin/` - CDK app entry point; it creates `BackendStack` and `EdgeWafStack`
   - `lib/` - CDK stack definitions and reusable constructs
   - `lambdas/` - Lambda function handlers (Python, plus the Node.js Session Runner)
   - `agents/` - LangGraph agents (Research, Drafting, Analysis, Pipeline Manager, and the single-source Tool Agent) that run on Amazon Bedrock AgentCore Runtime, with shared schemas, CEI UI presenters, prompts, and tools
   - `test/` - Jest tests for the CDK stacks

2. **frontend/** - Vite + React 19 + Tailwind CSS 4 single-page dashboard
   - `src/pages/` - Routed pages (react-router)
   - `src/components/` - UI components, including the CEI UI renderer
   - `src/lib/` - API clients, Cognito auth, and the SSE stream reducer
   - `public/` - Static assets

3. **deploy/** - The Python deploy tool that `deploy.sh` starts

4. **archive/** - The old Next.js test harness, kept for reference only

5. **docs/** - Project documentation
   - `media/` - Images, diagrams, and GIFs for documentation

6. **.agents/** - The working decision log, roadmap, specs, and build phase files

---

## Troubleshooting

### Deployment Fails

**Issue**: `./deploy.sh` or the CodeBuild build fails

**Solutions**:
1. Run `./deploy.sh --dry-run` to check the saved `backend/cei.deploy.json` with no AWS writes
2. Verify AWS credentials: `aws sts get-caller-identity`
3. Read the CodeBuild build log (the deploy tool shows it live); look for cdk-nag findings or CloudFormation errors
4. Make sure the selected Bedrock models are available in the deploy Region
5. Review CloudFormation events for `BackendStack` and `EdgeWafStack` (in `us-east-1`) in the AWS Console
6. See the [Deployment Guide](./docs/deploymentGuide.md) for each step and its permissions

### Frontend Can't Connect to Backend

**Issue**: Frontend shows CORS errors or can't reach the sessions API or the agents

**How CORS is wired (deploy time)**: every layer uses one origin list built in `backend/lib/backend-stack.ts`: the `SiteUrl` output, plus `http://localhost:5173` only when `allowLocalhostCallback` is set.
- **Sessions API** (`/sessions`, `/sessions/{id}`, `/sessions/{id}/history`): API Gateway answers the `OPTIONS` preflight (Authorization, Content-Type, Accept). The Lambdas echo the request origin from `ALLOWED_ORIGINS` on every response. Gateway errors (401/403/429/4xx/5xx) carry `Access-Control-Allow-Origin: <SiteUrl>`, so a localhost origin sees those specific errors as CORS failures.
- **Agents** (browser → `AgentProxy` Lambda function URL → AgentCore runtime): the browser never calls `bedrock-agentcore.*` directly, because AgentCore documents no CORS support and its JWT authorizer would reject a preflight (preflights never carry `Authorization`). The SPA POSTs to `<AgentProxyUrl><agent>` (`research`, `drafting`, `analysis`, `pipeline`). The function URL's CORS (POST; authorization, content-type, accept, session-id header; no credentials; max-age 3600) is answered by the Lambda service itself. `backend/lambdas/agent-proxy/handler.py` checks for a bearer token and a valid session id, then forwards the caller's own token to the runtime, where AgentCore's Cognito authorizer and `identity.py` verify it. The proxy URLs reach the SPA through `/config.json` `agentUrls`, and the site CSP `connect-src` allows `https://*.lambda-url.<region>.on.aws`. The runtimes still get `CEI_ALLOWED_ORIGINS` (`app_factory.py`), which only matters if a browser ever calls them directly.

**Solutions**:
1. Check `/config.json` on the deployed site: it must contain `apiBaseUrl` and four `agentUrls` that start with the `AgentProxyUrl` stack output.
2. In DevTools, check the `OPTIONS` request. It must return 200 with `Access-Control-Allow-Origin` equal to the site origin.
3. If an agent call fails, check the `AgentProxyLogs` log group: each request logs the agent, status and duration (never the token, session id or body). A 401 there means AgentCore rejected the token; sign in again.
4. Confirm the site origin matches `SiteUrl` exactly (scheme and host, no trailing slash).

### Local development

1. Copy `frontend/public/config.example.json` to `frontend/public/config.json` and fill in the Cognito values from the stack outputs. Keep `"apiBaseUrl": "/api"` and leave `agentUrls` out, so agent calls use the `/api/agents/*` dev proxy.
2. Create `frontend/.env.local` with `CEI_API_BASE_URL=<ApiBaseUrl stack output>` (e.g. `https://<id>.execute-api.<region>.amazonaws.com/<stage>`). The dev server then proxies `/api/sessions/*` to the deployed API, so no CORS applies. Without it, `npm run dev` prints a warning, and chat history and resume will not work.
3. Start the agents locally on ports 8080 (research), 8081 (drafting), 8083 (analysis) and 8084 (pipeline) via `CEI_RUNTIME_PORT`. Leave `CEI_ALLOWED_ORIGINS` unset; the requests are same-origin through the proxy.

### Security Scan Failures

**Issue**: cdk-nag or security scans report violations

**Solutions**:
1. Review findings and fix resource configurations
2. Add suppressions with ADR-format reasons if intentional (`ADR: <title> | Rationale: <why> | Alternative: <rejected>`)
3. Run `/cic-security-check` in Claude Code, or use the `cic-security` agent to review and fix issues

### Need More Help?

- **Operations and monitoring**: See [Operations](./docs/operations.md) and [Observability](./docs/observability.md)
- **Deployed resources**: See [Provisioned Resources](./docs/provisioned-resources.md)
- **Design decisions**: See `.agents/decision-log.md`
- **GitHub Issues**: File issues in this repository

---

## Credits

This application was developed by the ASU AI Cloud Innovation Center (CIC) build team:

- <a href="TO_BE_SUPPLIED" target="_blank">Aaditya Jindal</a> – Cloud Developer
- <a href="TO_BE_SUPPLIED" target="_blank">Sreeram Sravana Prasad</a> – Cloud Developer
- <a href="TO_BE_SUPPLIED" target="_blank">Sandrah Bosaire</a> – UI/UX Developer

> **TO BE SUPPLIED:** the LinkedIn URL for each team member, and any other teams, supporters, or organizations to acknowledge.

Built with support from Amazon Web Services (AWS) and the ASU AI Cloud Innovation Center.

---

## License

This project is licensed under the MIT License - see the [LICENSE](./LICENSE) file for details.
