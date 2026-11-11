import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import { Construct } from 'constructs';
import { NagSuppressions } from 'cdk-nag';
import { suppressXrayWildcard } from './tracing';

/** F9: the four specialist agents a classified or deterministic request can be routed to. */
export const ROUTED_AGENTS = ['research', 'drafting', 'analysis', 'pipeline_manager'] as const;
export type RoutedAgent = (typeof ROUTED_AGENTS)[number];

export interface SessionRouterProps {
  /** Validated deployment default; absent only during environment-agnostic synth. */
  defaultModelId?: string;
  /** B18: deploy.sh/CDK-supplied extra blocklist patterns, merged with the fixed D3 defaults (shared/config.py). */
  blockedModelIds?: string[];
  /** F9/B17: the fixed classifier model id (deploy-config `routerModelId`), independent of any user-selected model. */
  routerModelId?: string;
  /** F9/B80: confidence below this returns a ChoicePicker instead of routing. Implementation default: see handler.py. */
  confidenceThreshold?: number;
  /** F8/B51: the one Guardrail every model call (agents and the classifier) uses. */
  guardrailId?: string;
  guardrailVersion?: string;
  /** F9: scoped `bedrock:ApplyGuardrail` grant for the classifier call; undefined => no grant, classifier fails closed. */
  guardrailArn?: string;
  role: iam.Role;
  pythonDepsLayer: lambda.ILayerVersion;
  metadataTable: dynamodb.ITable;
  activeRunsTable: dynamodb.ITable;
  userPoolId: string;
  spaClientId: string;
  tokenEndpoint: string;
  clientId: string;
  secretArn: string;
  scopes: string[];
  /** F9: one AgentCore invocation URL per routed agent (research/drafting/analysis/pipeline_manager). */
  runtimeUrls: Record<RoutedAgent, string>;
}

/** handler.py's `_RUNTIME_URL_ENV`; kept next to the construct so both sides change together. */
const RUNTIME_URL_ENV: Record<RoutedAgent, string> = {
  research: 'RESEARCH_RUNTIME_URL',
  drafting: 'DRAFTING_RUNTIME_URL',
  analysis: 'ANALYSIS_RUNTIME_URL',
  pipeline_manager: 'PIPELINE_RUNTIME_URL',
};

/** The Router has no public URL; the Runner alone must receive invoke permission. */
export class SessionRouter extends Construct {
  public readonly function: lambda.Function;

  constructor(scope: Construct, id: string, props: SessionRouterProps) {
    super(scope, id);
    const stack = cdk.Stack.of(this);
    const logGroup = new logs.LogGroup(this, 'Logs', { retention: logs.RetentionDays.ONE_MONTH, removalPolicy: cdk.RemovalPolicy.DESTROY });
    logGroup.grantWrite(props.role);
    props.role.addToPolicy(new iam.PolicyStatement({ actions: ['dynamodb:GetItem'], resources: [props.metadataTable.tableArn] }));
    // F9: the same table also holds the short-lived route_choice (B80 ChoicePicker) record UiActions reads.
    props.role.addToPolicy(new iam.PolicyStatement({ actions: ['dynamodb:GetItem', 'dynamodb:UpdateItem'], resources: [props.activeRunsTable.tableArn] }));
    if (props.routerModelId) {
      // F9/B17: bedrock:InvokeModel scoped to the one fixed classifier model, never a wildcard.
      props.role.addToPolicy(new iam.PolicyStatement({
        actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
        resources: [
          `arn:${stack.partition}:bedrock:${stack.region}:${stack.account}:inference-profile/${props.routerModelId}`,
          `arn:${stack.partition}:bedrock:*::foundation-model/${props.routerModelId}`,
        ],
      }));
      const escapedModelId = props.routerModelId.replace(/[.*+?^${}()|[\]\\/]/g, '\\$&');
      NagSuppressions.addResourceSuppressions(props.role, [{
        id: 'AwsSolutions-IAM5',
        // ADR: regex over the partition, same as tool-gateway.ts | Rationale: cdk-nag reports
        // "<AWS::Partition>" by default but a literal partition under the cdk.json feature flags the CLI applies.
        appliesTo: [{ regex: `/^Resource::arn:(<AWS::Partition>|aws[a-z-]*):bedrock:\\*::foundation-model\\/${escapedModelId}$/` }],
        reason: 'ADR: Region wildcard on the foundation-model ARN | Rationale: foundation-model ARNs are regional '
          + 'but the fixed classifier model may be served from more than one Region; the model id itself, the '
          + 'only part that matters for least privilege, is not a wildcard | Alternative: per-Region ARNs rejected '
          + '-- the serving Region is AWS-managed, not a deploy-time setting',
      }], true);
    }
    if (props.guardrailArn) {
      // F8/B51: same ApplyGuardrail action the agent runtimes use (agent-runtimes.ts), scoped to one Guardrail ARN.
      props.role.addToPolicy(new iam.PolicyStatement({ actions: ['bedrock:ApplyGuardrail'], resources: [props.guardrailArn] }));
    }
    this.function = new lambda.Function(this, 'Function', {
      tracing: lambda.Tracing.ACTIVE,
      runtime: lambda.Runtime.PYTHON_3_14,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'lambdas', 'session-router')),
      role: props.role,
      logGroup,
      layers: [props.pythonDepsLayer],
      timeout: cdk.Duration.seconds(30),
      environment: {
        ...(props.defaultModelId ? { CEI_DEFAULT_MODEL_ID: props.defaultModelId } : {}),
        CEI_BLOCKED_MODELS: (props.blockedModelIds ?? []).join(','),
        ...(props.routerModelId ? { CEI_ROUTER_MODEL_ID: props.routerModelId } : {}),
        ...(props.confidenceThreshold !== undefined ? { CEI_ROUTER_CONFIDENCE_THRESHOLD: String(props.confidenceThreshold) } : {}),
        ...(props.guardrailId ? { CEI_GUARDRAIL_ID: props.guardrailId } : {}),
        ...(props.guardrailVersion ? { CEI_GUARDRAIL_VERSION: props.guardrailVersion } : {}),
        USER_POOL_ID: props.userPoolId,
        SPA_CLIENT_ID: props.spaClientId,
        METADATA_TABLE: props.metadataTable.tableName,
        ACTIVE_RUNS_TABLE: props.activeRunsTable.tableName,
        ROUTER_CLIENT_ID: props.clientId,
        ROUTER_SECRET_ARN: props.secretArn,
        TOKEN_ENDPOINT: props.tokenEndpoint,
        TOOL_SCOPES: props.scopes.join(','),
        // F9: handler.py's _RUNTIME_URL_ENV maps "agent" -> one of these four.
        ...Object.fromEntries(ROUTED_AGENTS.map((agent) => [RUNTIME_URL_ENV[agent], props.runtimeUrls[agent]])),
      },
    });
    suppressXrayWildcard(props.role);
  }
}
