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

// F11: each runtime reads its own memory id env var; opt-in recall flags (CEI_<AGENT>_LONG_TERM_ENABLED)
// default off and are not set here, matching Research's existing off-by-default convention.
const MEMORY_ID_ENV: Record<(typeof AGENT_RUNTIMES)[number]['id'], string> = {
  Research: 'CEI_RESEARCH_MEMORY_ID',
  Drafting: 'CEI_DRAFTING_MEMORY_ID',
  Analysis: 'CEI_ANALYSIS_MEMORY_ID',
  Pipeline: 'CEI_PIPELINE_MEMORY_ID',
};

// ADR: AgentCore Runtime is arm64 (Graviton) + PYTHON_3_12 (agentcore.json) | Rationale: CodeZip deps must be
// manylinux aarch64 wheels for that interpreter, so they are resolved for the target, not the build host.
// manylinux_2_28 (glibc 2.28): numpy 2.5 ships no manylinux2014 aarch64 wheels; the runtime image's glibc is newer
const UV_INSTALL_ARGS = [
  '--python-platform', 'aarch64-manylinux_2_28',
  '--python-version', '3.12',
  '--only-binary', ':all:',
];

function copySources(outputDir: string, extraDirs: string[] = [], extraFiles: string[] = []): void {
  for (const dir of [...SOURCE_DIRS, ...extraDirs]) {
    fs.cpSync(path.join(AGENTS_DIR, dir), path.join(outputDir, dir), {
      recursive: true,
      filter: (src) => !/(__pycache__|\.pyc$|\.pytest_cache|[\\/]tests([\\/]|$))/.test(src),
    });
  }
  for (const file of [...ENTRYPOINT_FILES, ...extraFiles]) {
    fs.copyFileSync(path.join(AGENTS_DIR, file), path.join(outputDir, file));
  }
}

export class LocalUvBundling implements cdk.ILocalBundling {
  constructor(private readonly extraDirs: string[] = [], private readonly extraFiles: string[] = []) {}

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
    copySources(outputDir, this.extraDirs, this.extraFiles);
    return true;
  }
}

/** AgentCore data-plane origin (the agent proxy Lambda forwards to it; the SPA never calls it directly). */
export function agentCoreDataPlaneOrigin(stack: cdk.Stack): string {
  return `https://bedrock-agentcore.${stack.region}.${stack.urlSuffix}`;
}

// ADR: build the invocation URL with intrinsics | Rationale: the runtime ARN is a deploy-time token, so
// encodeURIComponent cannot run on it; ':' and '/' are the only reserved characters an AgentCore runtime ARN holds
function runtimeUrl(stack: cdk.Stack, runtimeArn: string, operation: 'invocations' | 'stopruntimesession'): string {
  const colonsEncoded = cdk.Fn.join('%3A', cdk.Fn.split(':', runtimeArn));
  const encodedArn = cdk.Fn.join('%2F', cdk.Fn.split('/', colonsEncoded));
  return cdk.Fn.join('', [
    agentCoreDataPlaneOrigin(stack),
    '/runtimes/',
    encodedArn,
    `/${operation}?qualifier=DEFAULT`,
  ]);
}

function invocationUrl(stack: cdk.Stack, runtimeArn: string): string {
  return runtimeUrl(stack, runtimeArn, 'invocations');
}

/** Job agent_type values (shared/persistence/jobs.py AGENT_TYPES) for each runtime. */
export const JOB_AGENT_TYPES = {
  Research: 'research', Drafting: 'drafting', Analysis: 'analysis', Pipeline: 'pipeline_manager',
} as const;

export interface AgentRuntimesProps {
  /** Validated deployment default; absent only during environment-agnostic synth. */
  readonly defaultModelId?: string;
  /** B18: deploy.sh/CDK-supplied extra blocklist patterns, merged with the fixed D3 defaults (shared/config.py). */
  readonly blockedModelIds?: string[];
  /** F8/B51: input-only PII Guardrail (model-guardrail.ts); undefined => get_model() attaches no Guardrail. */
  readonly guardrailId?: string;
  readonly guardrailVersion?: string;
  readonly guardrailArn?: string;
  readonly userPool: cognito.IUserPool;
  readonly spaClient: cognito.IUserPoolClient;
  readonly chatMetadataTable: dynamodb.ITable;
  readonly chatMetadataIndexArns: string[];
  readonly chatHistoryTable: dynamodb.ITable;
  readonly codeInterpreter: agentcore.CodeInterpreterCustom;
  /** CDK-created attachments bucket; its generated name reaches the agents as CEI_ATTACHMENTS_BUCKET. */
  readonly attachmentsBucket: s3.IBucket;
  /** B110: Managed Knowledge Base id, for the runtime's own bedrock:Retrieve call (CEI_KNOWLEDGE_BASE_ID). */
  readonly knowledgeBaseId: string;
  /** B110: Managed Knowledge Base ARN, to scope the bedrock:Retrieve grant to this one resource. */
  readonly knowledgeBaseArn: string;
  /**
   * Browser origins allowed to call the runtimes directly (site URL, plus localhost when enabled). Reaches the
   * container as CEI_ALLOWED_ORIGINS, where shared/runtime/app_factory.py answers the CORS preflight.
   */
  readonly allowedOrigins: string[];
  /** B44/B45: mirrors ToolGateway's prop of the same name -- true only when the Region gate and the deploy config both allow Web Search (reaches the container as CEI_WEB_SEARCH_ENABLED). */
  readonly webSearchEnabled?: boolean;
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
  /**
   * StopRuntimeSession URLs keyed by job agent_type, for user-initiated job cancellation. The jobs Lambda calls
   * them with the caller's own bearer token, because the runtimes accept only SPA user tokens.
   */
  public readonly stopUrls: Record<string, string> = {};
  /** F19/B109: the one shared AgentCore Memory resource's id/ARN, for the delete-worker's cascade. */
  public readonly memoryId: string;
  public readonly memoryArn: string;

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
      description: 'Checkpoints and per-user specialist memory',
      memoryStrategies: [
        { userPreferenceMemoryStrategy: { name: 'user_preferences', namespaceTemplates: ['/users/{actorId}/'] } },
        { semanticMemoryStrategy: { name: 'semantic_facts', namespaceTemplates: ['/users/{actorId}/'] } },
      ],
    });
    researchMemory.applyRemovalPolicy(cdk.RemovalPolicy.RETAIN);
    this.memoryId = researchMemory.attrMemoryId;
    this.memoryArn = researchMemory.attrMemoryArn;
    const environmentVariables: Record<string, string> = {
      CEI_AWS_REGION: stack.region,
      ...(props.defaultModelId ? { CEI_DEFAULT_MODEL_ID: props.defaultModelId } : {}),
      CEI_BLOCKED_MODELS: (props.blockedModelIds ?? []).join(','),
      ...(props.guardrailId ? { CEI_GUARDRAIL_ID: props.guardrailId } : {}),
      ...(props.guardrailVersion ? { CEI_GUARDRAIL_VERSION: props.guardrailVersion } : {}),
      CEI_COGNITO_USER_POOL_ID: props.userPool.userPoolId,
      CEI_COGNITO_REGION: stack.region,
      CEI_COGNITO_APP_CLIENT_IDS: appClientIds,
      CEI_CHAT_METADATA_TABLE: props.chatMetadataTable.tableName,
      CEI_CHAT_HISTORY_TABLE: props.chatHistoryTable.tableName,
      CEI_CODE_INTERPRETER_ID: props.codeInterpreter.codeInterpreterId,
      CEI_ATTACHMENTS_BUCKET: props.attachmentsBucket.bucketName,
      // B110: retrieval is runtime code (not a Gateway target) -- the graph step calls bedrock:Retrieve
      // itself with an andAll user_id+session_id filter; the model only ever supplies the query text.
      CEI_KNOWLEDGE_BASE_ID: props.knowledgeBaseId,
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
      // B44/B86: shared/tools/data_sources.all_langchain_tools() only adds the web search tool when this is "true"
      CEI_WEB_SEARCH_ENABLED: props.webSearchEnabled ? 'true' : 'false',
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
          ? {
              ...environmentVariables,
              [MEMORY_ID_ENV[def.id]]: researchMemory.attrMemoryId,
              CEI_REQUIRE_TOOL_GATEWAY: 'true',
            }
          : { ...environmentVariables, [MEMORY_ID_ENV[def.id]]: researchMemory.attrMemoryId },
      });
      // F11: all four runtimes can retrieve from the one shared Memory resource; only
      // Research writes conversation events into it (B37 strategies, opt-in recall per agent).
      runtime.role.addToPrincipalPolicy(new iam.PolicyStatement({
        actions: def.id === 'Research'
          ? ['bedrock-agentcore:CreateEvent', 'bedrock-agentcore:ListEvents', 'bedrock-agentcore:DeleteEvent',
            'bedrock-agentcore:RetrieveMemoryRecords']
          : ['bedrock-agentcore:RetrieveMemoryRecords'],
        resources: [researchMemory.attrMemoryArn],
      }));
      this.grantData(runtime, props);
      props.codeInterpreter.grantUse(runtime);
      runtime.role.addToPrincipalPolicy(
        new iam.PolicyStatement({
          actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
          // B18/user decision (2026-11-10): users may pick any Bedrock model, not just Anthropic's; the
          // D3 blocklist + discovery filter (shared/config.py, model_discovery.py) is the actual control.
          resources: [
            `arn:${stack.partition}:bedrock:*::foundation-model/*`,
            `arn:${stack.partition}:bedrock:*:${stack.account}:inference-profile/*`,
            `arn:${stack.partition}:bedrock:*:${stack.account}:application-inference-profile/*`,
          ],
        }),
      );
      // F8/B51: the native guardrailConfig on every get_model() call (shared/utils.py) needs this on the
      // model-invoking role; scoped to the one Guardrail ARN, not a wildcard.
      if (props.guardrailArn) {
        runtime.role.addToPrincipalPolicy(new iam.PolicyStatement({
          actions: ['bedrock:ApplyGuardrail'], resources: [props.guardrailArn],
        }));
      }
      NagSuppressions.addResourceSuppressions(
        runtime,
        [
          {
            id: 'AwsSolutions-IAM5',
            reason:
              'ADR: wildcard ARNs on the AgentCore runtime role | Rationale: (1) Bedrock: the model id is a user choice of any non-blocklisted model (B18, user decision 2026-11-10), spanning every foundation model plus every account inference profile and application inference profile, not a fixed deploy-time set — the D3 blocklist and the discovery filter (shared/config.py, shared/runtime/model_discovery.py) are the real control, not the IAM resource; (2) the CDK-owned Runtime construct grants its own CloudWatch Logs/X-Ray/workload-identity permissions with wildcard resources; (3) S3: object keys in the attachments bucket are per-session and created at runtime, so the object ARN is <bucket>/* scoped to that single bucket | Alternative: per-model ARNs rejected — the model id is a user-facing runtime choice, not a fixed deploy-time set',
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
      this.stopUrls[JOB_AGENT_TYPES[def.id]] = runtimeUrl(stack, runtime.agentRuntimeArn, 'stopruntimesession');
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

  /**
   * F16: the Pipeline runtime records its background jobs in the Job ID Store and holds the user's single
   * background slot in the Active-run table (B22). Exactly the actions shared/runtime/durable_jobs.py calls.
   */
  public configureJobStore(jobTable: dynamodb.ITable, activeRunsTable: dynamodb.ITable): void {
    const runtime = this.runtimes.Pipeline;
    const resource = runtime.node.defaultChild as agentcore.CfnRuntime;
    resource.addPropertyOverride('EnvironmentVariables.CEI_JOB_TABLE', jobTable.tableName);
    resource.addPropertyOverride('EnvironmentVariables.CEI_ACTIVE_RUNS_TABLE', activeRunsTable.tableName);
    runtime.role.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem'], resources: [jobTable.tableArn],
    }));
    runtime.role.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['dynamodb:GetItem', 'dynamodb:UpdateItem'], resources: [activeRunsTable.tableArn],
    }));
  }

  /**
   * F8 follow-up: shared/runtime/turn_config.py's owner-scoped saved-default lookup, used only for a chat
   * with no stored model_id. Read-only GetItem on the same table ModelSettings/session-metadata-handler use;
   * called after ModelSettings exists (backend-stack.ts), same post-construction pattern as configureJobStore.
   */
  public configureUserMetadataTable(table: dynamodb.ITable): void {
    for (const def of AGENT_RUNTIMES) {
      const runtime = this.runtimes[def.id];
      const resource = runtime.node.defaultChild as agentcore.CfnRuntime;
      resource.addPropertyOverride('EnvironmentVariables.CEI_USER_METADATA_TABLE', table.tableName);
      table.grant(runtime.role, 'dynamodb:GetItem');
    }
  }

  /**
   * live-drafting: the Drafting runtime renders and persists its own exports itself (drafting/nodes.py
   * export_documents -> shared/persistence/exports.py ExportRepository: save() for a new export, and
   * download_bytes()/list_for_session() to recover the last draft on a cold thread). Drafting-only --
   * no other runtime saves exports yet.
   *
   * ADR: no writer-fence wiring here | Rationale: drafting/nodes.py calls ExportRepository directly, not
   * drafting/export_service.py's generate_reviewed_export, so it never constructs a
   * shared/persistence/writer_fence.py WriterFence; that module's own docstring says "Do not activate
   * this protocol yet" pending two documented idempotency gaps, and no runtime here needs it provisioned.
   */
  public configureDraftingExports(exportsTable: dynamodb.ITable, exportsBucket: s3.IBucket): void {
    const runtime = this.runtimes.Drafting;
    const resource = runtime.node.defaultChild as agentcore.CfnRuntime;
    resource.addPropertyOverride('EnvironmentVariables.CEI_EXPORT_METADATA_TABLE', exportsTable.tableName);
    resource.addPropertyOverride('EnvironmentVariables.CEI_EXPORT_BUCKET', exportsBucket.bucketName);
    // shared/persistence/exports.py ExportRepository: save() (PutItem + put_object + UpdateItem) and a later
    // turn's get()/list_for_session()/download_bytes() (GetItem/Query + S3 GetObject) -- same DynamoDB
    // actions as ExportsHandler's own grants, plus PutObject for save() and GetObject for cold recovery.
    exportsTable.grant(runtime.role, 'dynamodb:PutItem', 'dynamodb:UpdateItem', 'dynamodb:GetItem', 'dynamodb:Query');
    runtime.role.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['s3:PutObject', 's3:GetObject'], resources: [exportsBucket.arnForObjects('exports/*')],
    }));
    NagSuppressions.addResourceSuppressions(runtime, [{
      id: 'AwsSolutions-IAM5',
      reason: 'ADR: export object read/write scope | Rationale: report object keys (shared/persistence/exports.py) are generated at runtime under one bucket prefix, the same pattern as ExportsHandler | Alternative: fixed object ARNs rejected because exports do not exist at deployment',
    }], true);
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
    // B110: the deterministic retrieval graph step calls bedrock:Retrieve directly, scoped to this one KB
    runtime.role.addToPrincipalPolicy(
      new iam.PolicyStatement({ actions: ['bedrock:Retrieve'], resources: [props.knowledgeBaseArn] }),
    );
  }
}
