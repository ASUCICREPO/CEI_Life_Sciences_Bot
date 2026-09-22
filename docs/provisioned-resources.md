# Provisioned Resources — CEI Life Sciences Research Agent

| Resource | Service | Status | Notes |
|---|---|---|---|
| Research Agent Runtime | AgentCore Runtime | Provisioned via CDK | Entry point: `main.py`; handler: `handler` |
| Code Interpreter | AgentCore Code Interpreter | Provisioned via CDK | Custom resource, Public Network Mode; ID stored in `CEI_CODE_INTERPRETER_ID` |
| AgentCore Memory (checkpointer) | AgentCore Memory | SDK-managed | `AgentCoreMemorySaver`; memory ID `research-{user_id}`; no explicit CDK resource needed |
| AgentCore Memory (store) | AgentCore Memory | SDK-managed | `AgentCoreMemoryStore`; memory ID `research-store-{user_id}`; no explicit CDK resource needed |
| Claude Sonnet 5 | Amazon Bedrock | Model access required | Model ID: `anthropic.claude-sonnet-5-v1`; enable in Bedrock console per region |
| Claude Sonnet 3.7 (fallback) | Amazon Bedrock | Model access required | Fallback model; enable in Bedrock console per region |
| Packages S3 Bucket | S3 | Must be provisioned | Stores `life-sciences-packages.tar.gz` for Code Interpreter; bucket name set via `CEI_PACKAGES_S3_BUCKET` |
| Agent Execution Role | IAM | Provisioned via CDK | Needs permissions listed below |
| Chat History Store | DynamoDB | Provisioned via CDK | Table name: `ChatHistoryStore` |
| Chat Metadata Store | DynamoDB | Provisioned via CDK | Table name: `ChatMetadataStore` |
| Job ID Store | DynamoDB | Provisioned via CDK | Table name: `JobIdStore` |
| User Metadata Table | DynamoDB | Provisioned via CDK | Table name: `UserMetadataTable` |
| Attachment Metadata Store | DynamoDB | Provisioned via CDK | Table name: `AttachmentMetadataStore` |
| Attachments Bucket | S3 | Provisioned via CDK | Bucket name set via `CEI_ATTACHMENTS_BUCKET` |
| API Key Store | Secrets Manager | Provisioned per user | Secret prefix: `cei/user-keys` |

## Agent Execution Role — Required IAM Permissions

| Permission | Resource | Reason |
|---|---|---|
| `bedrock:InvokeModel`, `bedrock:InvokeModelWithResponseStream` | Bedrock model ARNs | LLM inference via `ChatBedrockConverse` |
| `bedrock-agentcore:*` | AgentCore runtime and memory ARNs | Agent invocation, memory read/write |
| `s3:GetObject`, `s3:PutObject` | Packages bucket, Attachments bucket | Code Interpreter package install; attachment access |
| `dynamodb:GetItem`, `dynamodb:PutItem`, `dynamodb:UpdateItem`, `dynamodb:Query` | All DynamoDB table ARNs | Session history, metadata, job tracking |
| `secretsmanager:GetSecretValue` | `arn:aws:secretsmanager:*:*:secret:cei/user-keys/*` | User-owned API keys for data source tools |
| `logs:CreateLogGroup`, `logs:CreateLogStream`, `logs:PutLogEvents` | CloudWatch log group ARN | Structured JSON logging |

## Environment Variables Required at Runtime

| Variable | Example Value | Source |
|---|---|---|
| `CEI_AWS_REGION` | `us-east-1` | CDK / task definition |
| `CEI_DEFAULT_MODEL_ID` | `anthropic.claude-sonnet-5-v1` | CDK / task definition |
| `CEI_CODE_INTERPRETER_ID` | `aws.codeinterpreter.v1` | CDK / task definition |
| `CEI_PACKAGES_S3_BUCKET` | `cei-code-interp-packages-prod` | CDK output |
| `CEI_PACKAGES_S3_KEY` | `life-sciences-packages.tar.gz` | CDK / task definition |
| `CEI_DIRECT_MODE` | `true` (CLI) / `false` (Lambda-backed) | Deployment context |
