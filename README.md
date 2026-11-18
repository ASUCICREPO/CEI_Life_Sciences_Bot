# CEI Life Sciences Bot

CEI Life Sciences Bot is an AI-powered assistant for life sciences researchers and founders. It gives each user deep research across public life sciences data sources, document drafting (grants, VC decks, journal articles, whitepapers), competitor and market analysis, and multi-step agent pipelines, all from one chat-based dashboard. It solves the problem of searching many separate scientific databases and drafting tools by hand, by bringing research, drafting, and analysis into one guided interface backed by Amazon Bedrock AgentCore.

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

> **TO DO (project team):** Add a GIF or screenshot of the dashboard as `docs/media/user-interface.gif`, then add the image link here.

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

![Architecture Diagram](./docs/media/architecture.png)

**Note:** this diagram shows an earlier design and is not updated. See [Architecture Deep Dive](./docs/architectureDeepDive.md) for the current target and the "Differences from the diagram" section there for what changed.

Today the deployed stack serves the dashboard as a static single-page app from Amazon S3 behind CloudFront and AWS WAF, with Amazon Cognito handling self-service sign-up (with an email-domain allowlist) and login. A single Regional REST API Gateway exposes the session, jobs, exports, models, and settings routes, plus a streaming route for live chat. A Node.js Session Runner Lambda relays that stream, and a Session Router Lambda sends each request to the right specialist agent — Research, Drafting, Analysis, or Pipeline Manager — each running on Amazon Bedrock AgentCore Runtime. The agents reach 12 public life sciences data sources plus NCBI through an AgentCore Gateway with Cedar access policies, and a deterministic Biopython/RDKit Lambda for lab calculations. DynamoDB stores session metadata, chat history, and background jobs; CloudWatch alarms and an SNS topic notify an admin email on errors and security events.

Several parts of the target architecture are still planned, not yet built: file attachments with a Knowledge Base for retrieval, long-term memory for every agent (today only Research uses it), user-owned API keys, account deletion (built, but behind a flag that is off by default), and full progressive streaming for every agent (today only Research streams its answer; the other agents return one completed response). See the [Architecture Deep Dive](./docs/architectureDeepDive.md) for the exact status of every component.

---

## Deployment Guide

For complete deployment instructions, see the [Deployment Guide](./docs/deploymentGuide.md).

**Quick Start:**
1. Sign in to the target AWS account with the AWS CLI (v2), and have Node.js 20+ and git available. See the Deployment Guide's Prerequisites section for the exact IAM permissions `deploy.sh` needs.
2. From the repo root, run `./deploy.sh` and answer the prompts (Region, default/router/blocked Bedrock models, allowed sign-up email domains, admin alert email), or pass them as flags. `deploy.sh` starts a CodeBuild build that builds the frontend, bootstraps CDK, and deploys the stacks — no build step runs on your machine.
3. When the build finishes, open the printed `SiteUrl` output in a browser and sign up with an email address in an allowed domain.

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
│   │   └── backend.ts          # CDK app entry point
│   ├── lib/
│   │   ├── backend-stack.ts    # main stack: API, Lambdas, Cognito, data stores, agent runtimes
│   │   ├── edge-waf-stack.ts   # CLOUDFRONT-scope WAF web ACL (always us-east-1)
│   │   └── constructs/         # one file per construct (agent runtimes, tool gateway, session router,
│   │                           # background jobs, life sciences operations, observability, ...)
│   ├── lambdas/                # Lambda handlers: session/jobs/exports/models APIs, one per data source,
│   │                           # lsops, auth triggers, session runner/router, ui-actions, watchdog, ...
│   ├── agents/                 # LangGraph agent code: research/, drafting/, analysis/, pipeline_manager/,
│   │                           # tool_agent/, shared/, and the local AgentCore CLI project (agentcore/)
│   ├── test/                   # Jest tests for the CDK stacks and constructs
│   ├── scripts/                # preflight.ts: Region/service capability check run inside CodeBuild
│   ├── cdk.json, package.json, tsconfig.json
├── frontend/
│   ├── src/                    # founder-ai-dashboard Vite SPA (React 19, Tailwind 4, react-router)
│   ├── public/                 # static assets, config.example.json
│   └── package.json
├── archive/
│   └── frontend-next/          # archived Next.js test harness (not deployed), kept for reference
├── docs/                       # human-facing documentation (this file links to all of it)
│   └── media/                  # images used in the docs
├── .agents/                    # agent-facing planning: decision-log.md, roadmap.md, specs/, phases/
├── deploy.sh                   # guided deployment script
├── buildspec*.yml              # CodeBuild build specs
├── LICENSE
└── README.md
```

### Directory Explanations:

1. **backend/** — AWS CDK (TypeScript) infrastructure and the Python/Node.js serverless functions and agents
   - `bin/` — CDK app entry point
   - `lib/` — the two CDK stacks (`BackendStack`, `EdgeWafStack`) and their constructs
   - `lambdas/` — one directory per Lambda function: session, jobs, exports, models/settings APIs, one
     handler per data source, Life Sciences Operations, Cognito triggers, the Session Runner/Router,
     `ui-actions`, and the background-job watchdog
   - `agents/` — the real LangGraph agent code for Research, Drafting, Analysis, Pipeline Manager, and the
     tool-agent profiles, run locally today through the AgentCore CLI
   - `test/` — Jest tests for the CDK stacks and constructs

2. **frontend/** — the `founder-ai-dashboard` Vite SPA, the production frontend
   - `src/` — React 19 components, pages, and the CEI UI / streaming client code
   - `public/` — static assets and the runtime `config.example.json`

3. **archive/frontend-next/** — the archived Next.js test harness that preceded the Vite SPA; kept for
   reference only and not deployed

4. **docs/** — project documentation
   - `media/` — images, diagrams, and GIFs for documentation

5. **.agents/** — agent-facing planning and specs (decision log, roadmap, detailed specs, and one file per
   build phase); this is the working source of truth during active development, see
   [Modification Guide](./docs/modificationGuide.md)

---

## Troubleshooting

### CDK Deployment Fails

**Issue**: the deployment started by `./deploy.sh` fails

**Solutions**:
1. Open the CodeBuild console log URL that `deploy.sh` prints (or that it prints again on failure) — every build step (`npm ci`, the Region pre-flight, the frontend build, `cdk bootstrap`, `cdk deploy`) runs there, not on your machine.
2. Check the pre-flight output in the build log: it fails fast if the chosen Region is missing a required service (AgentCore Runtime/Gateway/Memory/Policy, S3 Vectors, Bedrock Knowledge Bases) or if a selected Bedrock model is not available/entitled there.
3. Check cdk-nag findings in the build log and address or suppress them with an ADR-format reason.
4. Verify your AWS credentials have the permissions listed in the Deployment Guide's Prerequisites section (`deploy.sh` only needs IAM/CodeBuild/Secrets Manager/read-only CloudFormation permissions locally; `cdk bootstrap` and `cdk deploy` run under the CodeBuild role instead).

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
1. Review findings and fix resource configurations.
2. Add suppressions with ADR-format reasons if intentional: `ADR: <title> | Rationale: <why> | Alternative: <rejected>`.
3. Use the `cic-security` agent, or run `/cic-security-check`, to review and fix issues.

### Need More Help?

- **Operations**: see [Operations](./docs/operations.md) for monitoring and known validation gaps.
- **Observability**: see [Observability](./docs/observability.md) for alarms and dashboards.
- **GitHub Issues**: file issues in this repository for bugs or questions.

---

## Credits

This application was developed by:

- <a href="https://github.com/RETR0-OS" target="_blank">RETR0-OS</a>
- <a href="https://github.com/sreeram2001" target="_blank">sreeram2001</a>

> **TO DO (project team):** Replace these with full names and LinkedIn links, and add acknowledgments (for example ASU CIC and AWS).

---

## License

This project is licensed under the MIT License - see the [LICENSE](./LICENSE) file for details.
