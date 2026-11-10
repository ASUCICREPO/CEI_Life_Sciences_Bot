import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { spawnSync } from 'child_process';
import * as cdk from 'aws-cdk-lib';
import { aws_bedrockagentcore as agentcore } from 'aws-cdk-lib';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';

const AGENTS_DIR = path.join(__dirname, '..', '..', 'agents');

/** Source trees and entrypoints shipped in the CodeZip (everything else under backend/agents stays local). */
const SOURCE_DIRS = ['research', 'drafting', 'analysis', 'pipeline_manager', 'tool_agent', 'shared'];
const ENTRYPOINT_FILES = ['main.py', 'drafting_main.py', 'analysis_main.py', 'pipeline_main.py'];

// ADR: never ship local-only files | Rationale: .env holds developer credentials/settings; .venv is a Windows/host
// venv; agentcore/ is the local `agentcore dev` project (agentcore.json is local-development-only, docs/architectureDeepDive.md)
const EXCLUDES = [
  '.env', '.env.*', '.venv', '.pytest_cache', '__pycache__', '*.pyc', 'tests', 'demo_recordings',
  'agentcore', 'code_interpreter_packages',
];

/** The four specialist runtimes (names match backend/agents/agentcore/agentcore.json). */
export const AGENT_RUNTIMES = [
  { id: 'Research', runtimeName: 'CEIResearchAgent', entrypoint: 'main.py' },
  { id: 'Drafting', runtimeName: 'CEIDraftingAgent', entrypoint: 'drafting_main.py' },
  { id: 'Analysis', runtimeName: 'CEIAnalysisAgent', entrypoint: 'analysis_main.py' },
  { id: 'Pipeline', runtimeName: 'CEIPipelineManager', entrypoint: 'pipeline_main.py' },
] as const;

// ADR: AgentCore Runtime is arm64 (Graviton) + PYTHON_3_12 (agentcore.json) | Rationale: CodeZip deps must be
// manylinux aarch64 wheels for that interpreter, so they are resolved for the target, not the build host.
// manylinux_2_28 (glibc 2.28): numpy 2.5 ships no manylinux2014 aarch64 wheels; the runtime image's glibc is newer
const UV_INSTALL_ARGS = [
  '--python-platform', 'aarch64-manylinux_2_28',
  '--python-version', '3.12',
  '--only-binary', ':all:',
];

function copySources(outputDir: string): void {
  for (const dir of SOURCE_DIRS) {
    fs.cpSync(path.join(AGENTS_DIR, dir), path.join(outputDir, dir), {
      recursive: true,
      filter: (src) => !/(__pycache__|\.pyc$|\.pytest_cache|[\\/]tests([\\/]|$))/.test(src),
    });
  }
  for (const file of ENTRYPOINT_FILES) {
    fs.copyFileSync(path.join(AGENTS_DIR, file), path.join(outputDir, file));
  }
}

class LocalUvBundling implements cdk.ILocalBundling {
  tryBundle(outputDir: string): boolean {
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'cei-agent-reqs-'));
    const requirements = path.join(tmp, 'requirements.txt');
    // ADR: install from uv.lock (frozen) | Rationale: deployed deps are exactly the locked versions the tests ran against
    const exported = spawnSync(
      'uv',
      ['export', '--frozen', '--no-dev', '--no-emit-project', '--no-hashes', '--output-file', requirements],
      { cwd: AGENTS_DIR, stdio: ['ignore', 2, 2] },
    );
    if (exported.status !== 0) return false;
    const installed = spawnSync(
      'uv',
      ['pip', 'install', '-r', requirements, '--target', outputDir, ...UV_INSTALL_ARGS],
      { cwd: AGENTS_DIR, stdio: ['ignore', 2, 2] },
    );
    if (installed.status !== 0) return false;
    copySources(outputDir);
    return true;
  }
}

/** AgentCore data-plane origin (the agent proxy Lambda forwards to it; the SPA never calls it directly). */
export function agentCoreDataPlaneOrigin(stack: cdk.Stack): string {
  return `https://bedrock-agentcore.${stack.region}.${stack.urlSuffix}`;
}

// ADR: build the invocation URL with intrinsics | Rationale: the runtime ARN is a deploy-time token, so
// encodeURIComponent cannot run on it; ':' and '/' are the only reserved characters an AgentCore runtime ARN holds
function invocationUrl(stack: cdk.Stack, runtimeArn: string): string {
  const colonsEncoded = cdk.Fn.join('%3A', cdk.Fn.split(':', runtimeArn));
  const encodedArn = cdk.Fn.join('%2F', cdk.Fn.split('/', colonsEncoded));
  return cdk.Fn.join('', [
    agentCoreDataPlaneOrigin(stack),
    '/runtimes/',
    encodedArn,
    '/invocations?qualifier=DEFAULT',
  ]);
}

export interface AgentRuntimesProps {
  readonly userPool: cognito.IUserPool;
  readonly spaClient: cognito.IUserPoolClient;
  readonly chatMetadataTable: dynamodb.ITable;
  readonly chatMetadataIndexArns: string[];
  readonly chatHistoryTable: dynamodb.ITable;
  readonly codeInterpreter: agentcore.CodeInterpreterCustom;
  /** CDK-created attachments bucket; its generated name reaches the agents as CEI_ATTACHMENTS_BUCKET. */
  readonly attachmentsBucket: s3.IBucket;
  /**
   * Browser origins allowed to call the runtimes directly (site URL, plus localhost when enabled). Reaches the
   * container as CEI_ALLOWED_ORIGINS, where shared/runtime/app_factory.py answers the CORS preflight.
   */
  readonly allowedOrigins: string[];
}

/**
 * The four AgentCore runtimes with their deploy-time identity wiring (plan 3b4d4f64 decision 9):
 * - inbound Cognito JWT authorizer (layer 1) restricted to the SPA app client;
 * - the Authorization header forwarded so identity.py re-verifies the access token against the JWKS (layer 2);
 * - CEI_COGNITO_* settings — without them identity.py rejects every request (fail closed);
 * - least-privilege access to ChatMetadataStore / ChatHistoryStore (turns + checkpoint items).
 * There is no auth-bypass env var of any kind.
 */
export class AgentRuntimes extends Construct {
  public readonly runtimes: Record<(typeof AGENT_RUNTIMES)[number]['id'], agentcore.Runtime>;
  /**
   * AgentCore data-plane invocation URLs keyed by the SPA's agent name (research | drafting | analysis | pipeline);
   * handed to the agent proxy Lambda (AGENT_URLS), which the SPA calls via /config.json `agentUrls`.
   */
  public readonly invocationUrls: Record<Lowercase<(typeof AGENT_RUNTIMES)[number]['id']>, string>;

  constructor(scope: Construct, id: string, props: AgentRuntimesProps) {
    super(scope, id);
    const stack = cdk.Stack.of(this);

    // ADR: skip bundling under Jest | Rationale: unit tests must not run uv or Docker (same as PythonDepsLayer)
    const underJest = process.env.JEST_WORKER_ID !== undefined;
    const assetOptions = underJest
      ? { exclude: EXCLUDES }
      : {
          exclude: EXCLUDES,
          bundling: {
            image: cdk.DockerImage.fromRegistry('ghcr.io/astral-sh/uv:python3.12-bookworm-slim'),
            local: new LocalUvBundling(),
            command: [
              'bash', '-c',
              [
                'uv export --frozen --no-dev --no-emit-project --no-hashes --output-file /tmp/requirements.txt',
                `uv pip install -r /tmp/requirements.txt --target /asset-output ${UV_INSTALL_ARGS.join(' ')}`,
                `cp -r ${SOURCE_DIRS.join(' ')} ${ENTRYPOINT_FILES.join(' ')} /asset-output/`,
                'find /asset-output -name __pycache__ -prune -exec rm -rf {} +',
              ].join(' && '),
            ],
          },
        };

    // ADR: JSON list string | Rationale: pydantic-settings parses list[str] env values as JSON (config.cognito_app_client_ids)
    const appClientIds = cdk.Fn.join('', ['["', props.spaClient.userPoolClientId, '"]']);
    // B82 requires the maximum short-term retention. CloudFormation currently caps
    // EventExpiryDuration at 365 days; retain checkpoints across stack removal.
    const researchMemory = new agentcore.CfnMemory(this, 'ResearchMemory', {
      name: 'CEIResearchMemory', eventExpiryDuration: 365,
      description: 'Research short-term checkpoints scoped by user and session',
    });
    researchMemory.applyRemovalPolicy(cdk.RemovalPolicy.RETAIN);
    const environmentVariables: Record<string, string> = {
      CEI_AWS_REGION: stack.region,
      CEI_COGNITO_USER_POOL_ID: props.userPool.userPoolId,
      CEI_COGNITO_REGION: stack.region,
      CEI_COGNITO_APP_CLIENT_IDS: appClientIds,
      CEI_CHAT_METADATA_TABLE: props.chatMetadataTable.tableName,
      CEI_CHAT_HISTORY_TABLE: props.chatHistoryTable.tableName,
      CEI_CODE_INTERPRETER_ID: props.codeInterpreter.codeInterpreterId,
      CEI_ATTACHMENTS_BUCKET: props.attachmentsBucket.bucketName,
      // ADOT's opt-out=true keeps GenAI payloads on spans. Keep extraction enabled and
      // discard extracted OTLP logs so recognized GenAI content is not exported.
      AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT: 'false',
      OTEL_LOGS_EXPORTER: 'none',
      CEI_TELEMETRY_ENABLED: 'true',
      AGENT_OBSERVABILITY_ENABLED: 'true',
      OTEL_PYTHON_DISTRO: 'aws_distro',
      OTEL_PYTHON_CONFIGURATOR: 'aws_configurator',
      // HTTP client URL attributes can contain user queries or API keys. Instrument AWS SDK
      // calls instead; leave generic client auto-instrumentation off until URLs are sanitized.
      OTEL_PYTHON_DISABLED_INSTRUMENTATIONS: 'requests,urllib,urllib3,httpx,httpx2,aiohttp-client',
      // ADR: comma-joined string | Rationale: parsed by config.allowed_origins_list; same origin set as the
      // sessions API (API Gateway preflight + Lambda ALLOWED_ORIGINS) so all CORS layers agree
      CEI_ALLOWED_ORIGINS: cdk.Fn.join(',', props.allowedOrigins),
    };

    const runtimes = {} as Record<(typeof AGENT_RUNTIMES)[number]['id'], agentcore.Runtime>;
    const invocationUrls = {} as Record<Lowercase<(typeof AGENT_RUNTIMES)[number]['id']>, string>;
    for (const def of AGENT_RUNTIMES) {
      const runtime = new agentcore.Runtime(this, `${def.id}Runtime`, {
        runtimeName: def.runtimeName,
        tracingEnabled: true,
        description: `CEI ${def.id} agent runtime`,
        agentRuntimeArtifact: agentcore.AgentRuntimeArtifact.fromCodeAsset({
          path: AGENTS_DIR,
          runtime: agentcore.AgentCoreRuntime.PYTHON_3_12,
          entrypoint: [def.entrypoint],
          ...assetOptions,
        }),
        // Layer 1: AgentCore rejects any request without a valid access token from this pool + SPA client
        authorizerConfiguration: agentcore.RuntimeAuthorizerConfiguration.usingCognito(props.userPool, [props.spaClient]),
        // Layer 2 needs the raw bearer token in-process (identity.py verifies it against the JWKS again)
        requestHeaderConfiguration: { allowlistedHeaders: ['Authorization'] },
        environmentVariables: def.id === 'Research'
          ? { ...environmentVariables, CEI_RESEARCH_MEMORY_ID: researchMemory.attrMemoryId, CEI_REQUIRE_TOOL_GATEWAY: 'true' }
          : environmentVariables,
      });
      if (def.id === 'Research') {
        runtime.role.addToPrincipalPolicy(new iam.PolicyStatement({
          actions: ['bedrock-agentcore:CreateEvent', 'bedrock-agentcore:ListEvents', 'bedrock-agentcore:DeleteEvent'],
          resources: [researchMemory.attrMemoryArn],
        }));
      }
      this.grantData(runtime, props);
      props.codeInterpreter.grantUse(runtime);
      runtime.role.addToPrincipalPolicy(
        new iam.PolicyStatement({
          actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
          resources: [
            `arn:${stack.partition}:bedrock:${stack.region}:${stack.account}:inference-profile/us.anthropic.*`,
            `arn:${stack.partition}:bedrock:*::foundation-model/anthropic.*`,
          ],
        }),
      );
      NagSuppressions.addResourceSuppressions(
        runtime,
        [
          {
            id: 'AwsSolutions-IAM5',
            reason:
              'ADR: wildcard ARNs on the AgentCore runtime role | Rationale: (1) Bedrock: the agent and title models are cross-region inference profiles (us.anthropic.*) that route to foundation models in several Regions, so the model ARNs carry a Region wildcard limited to Anthropic models; (2) the CDK-owned Runtime construct grants its own CloudWatch Logs/X-Ray/workload-identity permissions with wildcard resources; (3) S3: object keys in the attachments bucket are per-session and created at runtime, so the object ARN is <bucket>/* scoped to that single bucket | Alternative: per-model ARNs rejected — the model id is a runtime setting and the routing Region set is AWS-managed',
          },
        ],
        true,
      );
      new cdk.CfnOutput(stack, `${def.id}RuntimeArn`, {
        value: runtime.agentRuntimeArn,
        description: `ARN of the ${def.runtimeName} AgentCore runtime (Cognito JWT inbound auth)`,
      });
      runtimes[def.id] = runtime;
      invocationUrls[def.id.toLowerCase() as Lowercase<typeof def.id>] = invocationUrl(stack, runtime.agentRuntimeArn);
    }
    this.runtimes = runtimes;
    this.invocationUrls = invocationUrls;
  }

  /** Connect only Research to the trusted Gateway after both constructs exist. */
  public configureResearchGateway(clientId: string, gatewayUrl: string): void {
    const resource = this.runtimes.Research.node.defaultChild as agentcore.CfnRuntime;
    resource.addPropertyOverride('EnvironmentVariables.CEI_GATEWAY_CLIENT_ID', clientId);
    resource.addPropertyOverride('EnvironmentVariables.CEI_TOOL_GATEWAY_URL', gatewayUrl);
    resource.addPropertyOverride('RequestHeaderConfiguration.RequestHeaderAllowlist', ['Authorization', 'X-Cei-Gateway-Token']);
  }

  /** Exactly the DynamoDB actions ConversationRecorder / ChatHistoryStore / checkpoint writer call. */
  private grantData(runtime: agentcore.Runtime, props: AgentRuntimesProps): void {
    // ADR: explicit statements, not Table.grant | Rationale: Table.grant appends `<table>/index/*` once a table has GSIs (IAM5)
    runtime.role.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem', 'dynamodb:Query'],
        resources: [props.chatMetadataTable.tableArn],
      }),
    );
    runtime.role.addToPrincipalPolicy(
      new iam.PolicyStatement({ actions: ['dynamodb:Query'], resources: props.chatMetadataIndexArns }),
    );
    runtime.role.addToPrincipalPolicy(
      new iam.PolicyStatement({
        // DeleteItem: checkpoints.py removes chunks left over from a larger previous snapshot (never turn items)
        actions: ['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:Query', 'dynamodb:DeleteItem'],
        resources: [props.chatHistoryTable.tableArn],
      }),
    );
    // ADR: identity-policy grant on the runtime role, not a bucket policy naming the role | Rationale: keeps the
    // dependency one-way (runtime -> bucket), so the bucket never references the runtimes and no cycle can form
    runtime.role.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ['s3:GetObject', 's3:PutObject'],
        resources: [props.attachmentsBucket.arnForObjects('*')],
      }),
    );
    runtime.role.addToPrincipalPolicy(
      new iam.PolicyStatement({ actions: ['s3:ListBucket'], resources: [props.attachmentsBucket.bucketArn] }),
    );
  }
}
