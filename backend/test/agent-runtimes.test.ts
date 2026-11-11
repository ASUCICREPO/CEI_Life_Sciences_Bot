import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Annotations, Match, Template } from 'aws-cdk-lib/assertions';
import { BackendStack } from '../lib/backend-stack';
import { AGENT_RUNTIMES } from '../lib/constructs/agent-runtimes';
import { DEFAULT_LIMITS } from '../lib/config/deploy-config';

const FIXTURE_ASSET_PATH = path.join(__dirname, 'fixtures', 'spa');

function build(): { stack: BackendStack; template: Template } {
  const app = new cdk.App();
  const stack = new BackendStack(app, 'RuntimeTestStack', {
    env: { region: 'us-east-1', account: '123456789012' },
    frontendAssetPath: FIXTURE_ASSET_PATH,
  });
  return { stack, template: Template.fromStack(stack) };
}

const { stack, template } = build();
const runtimes = Object.entries(template.findResources('AWS::BedrockAgentCore::Runtime'));
const userPoolId = Object.keys(template.findResources('AWS::Cognito::UserPool'))[0];
const spaClientId = Object.keys(template.findResources('AWS::Cognito::UserPoolClient'))[0];
const tableIds = Object.keys(template.findResources('AWS::DynamoDB::Table'));

describe('AgentCore runtimes: deploy-time Cognito settings', () => {
  test('passes the configured default model to every runtime', () => {
    const app = new cdk.App();
    const configured = new BackendStack(app, 'ConfiguredModelStack', {
      env: { region: 'us-east-1', account: '123456789012' },
      frontendAssetPath: FIXTURE_ASSET_PATH,
      config: { region: 'us-east-1', defaultModelId: 'example.model-v2', routerModelId: 'router',
        blockedModelIds: [], allowedEmailDomains: ['example.com'], adminEmail: 'admin@example.com',
        limits: DEFAULT_LIMITS, webSearchEnabled: false, allowLocalhostCallback: false },
    });
    const configuredRuntimes = Object.values(Template.fromStack(configured).findResources('AWS::BedrockAgentCore::Runtime')) as any[];
    expect(configuredRuntimes).toHaveLength(4);
    for (const runtime of configuredRuntimes) {
      expect(runtime.Properties.EnvironmentVariables.CEI_DEFAULT_MODEL_ID).toBe('example.model-v2');
    }
    const functions = Object.values(Template.fromStack(configured).findResources('AWS::Lambda::Function')) as any[];
    const routers = functions.filter((fn) => fn.Properties.Environment?.Variables?.TOOL_SCOPES);
    expect(routers).toHaveLength(1);
    expect(routers[0].Properties.Environment.Variables.CEI_DEFAULT_MODEL_ID).toBe('example.model-v2');
  });

  test('all four runtimes exist with the agentcore.json names', () => {
    const names = runtimes.map(([, r]) => (r as any).Properties.AgentRuntimeName).sort();
    expect(names).toEqual(AGENT_RUNTIMES.map((r) => r.runtimeName).sort());
  });

  test('delivers traces for all runtimes without extracting model content', () => {
    const sources = Object.values(template.findResources('AWS::Logs::DeliverySource')) as any[];
    expect(sources.filter((source) => source.Properties.LogType === 'TRACES')).toHaveLength(4);
    const destinations = Object.values(template.findResources('AWS::Logs::DeliveryDestination')) as any[];
    expect(destinations.filter((destination) => destination.Properties.DeliveryDestinationType === 'XRAY')).toHaveLength(4);
    template.resourceCountIs('AWS::XRay::ResourcePolicy', 1);
    for (const [, runtime] of runtimes) {
      expect((runtime as any).Properties.EnvironmentVariables.AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT).toBe('false');
      expect((runtime as any).Properties.EnvironmentVariables.OTEL_LOGS_EXPORTER).toBe('none');
      expect((runtime as any).Properties.EnvironmentVariables.OTEL_PYTHON_DISABLED_INSTRUMENTATIONS).toContain('requests');
      expect((runtime as any).Properties.EnvironmentVariables.CEI_TELEMETRY_ENABLED).toBe('true');
    }
  });

  test.each(runtimes.map(([id, r]) => [id, r] as const))('%s: inbound JWT authorizer pinned to the pool + SPA client', (_id, r) => {
    const jwt = (r as any).Properties.AuthorizerConfiguration.CustomJWTAuthorizer;
    expect(JSON.stringify(jwt.DiscoveryUrl)).toContain(userPoolId);
    expect(JSON.stringify(jwt.DiscoveryUrl)).toContain('openid-configuration');
    expect(jwt.AllowedClients).toEqual([{ Ref: spaClientId }]);
  });

  test.each(runtimes.map(([id, r]) => [id, r] as const))('%s: forwards Authorization and carries CEI_COGNITO_* env', (_id, r) => {
    const props = (r as any).Properties;
    expect(props.RequestHeaderConfiguration.RequestHeaderAllowlist).toEqual(
      props.AgentRuntimeName === 'CEIResearchAgent' ? ['Authorization', 'X-Cei-Gateway-Token'] : ['Authorization'],
    );
    const env = props.EnvironmentVariables;
    expect(env.CEI_COGNITO_USER_POOL_ID).toEqual({ Ref: userPoolId });
    expect(env.CEI_COGNITO_REGION).toBe('us-east-1');
    expect(env.CEI_COGNITO_APP_CLIENT_IDS).toEqual({ 'Fn::Join': ['', ['["', { Ref: spaClientId }, '"]']] });
    for (const name of ['CEI_CHAT_METADATA_TABLE', 'CEI_CHAT_HISTORY_TABLE']) {
      expect(tableIds).toContain(env[name].Ref);
    }
  });

  test.each(runtimes.map(([id, r]) => [id, r] as const))('%s: no auth-bypass env var', (_id, r) => {
    const keys = Object.keys((r as any).Properties.EnvironmentVariables);
    expect(keys.filter((k) => k !== 'OTEL_PYTHON_DISABLED_INSTRUMENTATIONS' && /DIRECT_MODE|LOCAL_DEMO|USER_ID|SKIP|BYPASS|DISABLE/i.test(k))).toEqual([]);
  });

  test.each(runtimes.map(([id, r]) => [id, r] as const))('%s: carries an explicit CEI_ALLOWED_ORIGINS browser allowlist', (_id, r) => {
    const value = (r as any).Properties.EnvironmentVariables.CEI_ALLOWED_ORIGINS;
    expect(value).toBeDefined();
    const json = JSON.stringify(value);
    expect(json).not.toContain('"*"');
    // localhost is only added behind config.allowLocalhostCallback, which this stack does not set
    expect(json).not.toContain('localhost');
  });

  test('runtime roles get exact (non-wildcard) DynamoDB table/index resources', () => {
    const policies = Object.values(template.findResources('AWS::IAM::Policy'));
    const dynamoStatements = policies
      .filter((p) => JSON.stringify((p as any).Properties.Roles).includes('AgentRuntimes'))
      .flatMap((p) => (p as any).Properties.PolicyDocument.Statement)
      .filter((s: any) => [s.Action].flat().some((a: string) => a.startsWith('dynamodb:')));
    expect(dynamoStatements.length).toBeGreaterThan(0);
    for (const s of dynamoStatements) {
      expect(JSON.stringify(s.Resource)).not.toMatch(/index\/\*|"\*"/);
      expect([s.Action].flat()).not.toContain('dynamodb:Scan');
    }
  });

  test('a runtime ARN output exists per runtime', () => {
    const outputs = Object.keys(template.toJSON().Outputs ?? {});
    for (const def of AGENT_RUNTIMES) {
      expect(outputs).toContain(`${def.id}RuntimeArn`);
    }
  });

  test('Research uses retained AgentCore Memory with the maximum event retention', () => {
    template.hasResource('AWS::BedrockAgentCore::Memory', {
      Properties: Match.objectLike({
        EventExpiryDuration: 365,
        MemoryStrategies: [
          { UserPreferenceMemoryStrategy: { Name: 'user_preferences', NamespaceTemplates: ['/users/{actorId}/'] } },
          { SemanticMemoryStrategy: { Name: 'semantic_facts', NamespaceTemplates: ['/users/{actorId}/'] } },
        ],
      }), DeletionPolicy: 'Retain',
    });
    const research = runtimes.find(([, r]) => (r as any).Properties.AgentRuntimeName === 'CEIResearchAgent')![1] as any;
    const memoryId = Object.keys(template.findResources('AWS::BedrockAgentCore::Memory'))[0];
    expect(research.Properties.EnvironmentVariables.CEI_RESEARCH_MEMORY_ID).toEqual({ 'Fn::GetAtt': [memoryId, 'MemoryId'] });
    expect(research.Properties.EnvironmentVariables.CEI_REQUIRE_TOOL_GATEWAY).toBe('true');
    const policies = Object.values(template.findResources('AWS::IAM::Policy')) as any[];
    expect(policies.some((p) => JSON.stringify(p.Properties.Roles).includes('AgentRuntimesResearchRuntime') &&
      p.Properties.PolicyDocument.Statement.some((s: any) => [s.Action].flat().includes('bedrock-agentcore:RetrieveMemoryRecords') &&
        JSON.stringify(s.Resource).includes(memoryId)))).toBe(true);
  });

  test('Drafting, Analysis and Pipeline get a memory id and retrieve-only access (F11)', () => {
    const memoryId = Object.keys(template.findResources('AWS::BedrockAgentCore::Memory'))[0];
    const policies = Object.values(template.findResources('AWS::IAM::Policy')) as any[];
    const cases: Array<[string, string, string]> = [
      ['CEIDraftingAgent', 'CEI_DRAFTING_MEMORY_ID', 'AgentRuntimesDraftingRuntime'],
      ['CEIAnalysisAgent', 'CEI_ANALYSIS_MEMORY_ID', 'AgentRuntimesAnalysisRuntime'],
      ['CEIPipelineManager', 'CEI_PIPELINE_MEMORY_ID', 'AgentRuntimesPipelineRuntime'],
    ];
    for (const [runtimeName, envVar, roleFragment] of cases) {
      const runtime = runtimes.find(([, r]) => (r as any).Properties.AgentRuntimeName === runtimeName)![1] as any;
      expect(runtime.Properties.EnvironmentVariables[envVar]).toEqual({ 'Fn::GetAtt': [memoryId, 'MemoryId'] });
      expect(runtime.Properties.EnvironmentVariables.CEI_REQUIRE_TOOL_GATEWAY).toBeUndefined();
      expect(policies.some((p) => JSON.stringify(p.Properties.Roles).includes(roleFragment) &&
        p.Properties.PolicyDocument.Statement.some((s: any) =>
          [s.Action].flat().includes('bedrock-agentcore:RetrieveMemoryRecords') &&
          !['bedrock-agentcore:CreateEvent'].some((a) => [s.Action].flat().includes(a)) &&
          JSON.stringify(s.Resource).includes(memoryId)))).toBe(true);
    }
  });

  test('every runtime gets the Guardrail id/version and the blocklist env var (F8)', () => {
    const guardrailId = Object.keys(template.findResources('AWS::Bedrock::Guardrail'))[0];
    for (const [, runtime] of runtimes) {
      const env = (runtime as any).Properties.EnvironmentVariables;
      expect(env.CEI_GUARDRAIL_ID).toEqual({ 'Fn::GetAtt': [guardrailId, 'GuardrailId'] });
      expect(env.CEI_GUARDRAIL_VERSION).toBeDefined();
      expect(env.CEI_BLOCKED_MODELS).toBeDefined();
    }
    const policies = Object.values(template.findResources('AWS::IAM::Policy')) as any[];
    expect(policies.some((p) => JSON.stringify(p.Properties.Roles).includes('AgentRuntimesResearchRuntime') &&
      p.Properties.PolicyDocument.Statement.some((s: any) =>
        [s.Action].flat().includes('bedrock:ApplyGuardrail') && !JSON.stringify(s.Resource).includes('"*"')))).toBe(true);
  });

  test('every runtime gets the UserMetadataTable name and a GetItem-only grant (F8 saved default)', () => {
    const userMetadataTableId = tableIds.find((id) => id.includes('UserMetadataTable'));
    expect(userMetadataTableId).toBeDefined();
    for (const [, runtime] of runtimes) {
      expect((runtime as any).Properties.EnvironmentVariables.CEI_USER_METADATA_TABLE).toEqual({ Ref: userMetadataTableId });
    }
    const policies = Object.values(template.findResources('AWS::IAM::Policy')) as any[];
    const roleFragments = [
      'AgentRuntimesResearchRuntime', 'AgentRuntimesDraftingRuntime',
      'AgentRuntimesAnalysisRuntime', 'AgentRuntimesPipelineRuntime',
    ];
    for (const roleFragment of roleFragments) {
      const onUserMetadata = policies
        .filter((p) => JSON.stringify(p.Properties.Roles).includes(roleFragment))
        .flatMap((p) => p.Properties.PolicyDocument.Statement)
        .filter((s: any) => JSON.stringify(s.Resource).includes('UserMetadataTable'));
      expect(onUserMetadata).toHaveLength(1);
      expect([onUserMetadata[0].Action].flat()).toEqual(['dynamodb:GetItem']);
    }
  });

  test('model-invocation policy allows any foundation model and inference profile, not Anthropic-only (user decision 2026-11-10)', () => {
    const policies = Object.values(template.findResources('AWS::IAM::Policy')) as any[];
    const roleFragments = [
      'AgentRuntimesResearchRuntime', 'AgentRuntimesDraftingRuntime',
      'AgentRuntimesAnalysisRuntime', 'AgentRuntimesPipelineRuntime',
    ];
    for (const roleFragment of roleFragments) {
      const invokeStatements = policies
        .filter((p) => JSON.stringify(p.Properties.Roles).includes(roleFragment))
        .flatMap((p) => p.Properties.PolicyDocument.Statement)
        .filter((s: any) => [s.Action].flat().includes('bedrock:InvokeModel'));
      expect(invokeStatements).toHaveLength(1);
      const resources = [invokeStatements[0].Resource].flat().map((r: unknown) => JSON.stringify(r));
      expect(resources.some((r) => r.includes('foundation-model/*'))).toBe(true);
      expect(resources.some((r) => r.includes(':inference-profile/*'))).toBe(true);
      expect(resources.some((r) => r.includes('application-inference-profile/*'))).toBe(true);
      expect(resources.join(' ')).not.toContain('anthropic');
    }
  });

  test('zero unsuppressed cdk-nag errors after adding the runtimes', () => {
    const errors = Annotations.fromStack(stack).findError('*', Match.stringLikeRegexp('AwsSolutions-.*'));
    expect(errors).toHaveLength(0);
  });
});
