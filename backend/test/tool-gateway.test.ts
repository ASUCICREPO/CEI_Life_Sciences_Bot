import * as fs from 'fs';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Annotations, Match, Template } from 'aws-cdk-lib/assertions';
import { BackendStack } from '../lib/backend-stack';
import { DeployConfig, DEFAULT_LIMITS } from '../lib/config/deploy-config';
import { assertNoScopeCollisions, cedarPermit, loadToolSchemas } from '../lib/constructs/tool-gateway';

const FIXTURE_ASSET_PATH = path.join(__dirname, 'fixtures', 'spa');
const SOURCE_IDS = ['pubmed', 'clinicaltrials', 'biorxiv', 'medrxiv', 'chembl', 'opentargets', 'cmscoverage', 'icd10', 'npi', 'pubchem', 'openfda', 'patentsview'];
const EXPECTED_SCOPES = [...SOURCE_IDS.map((id) => `src.${id}`), 'src.ncbi', 'common'];

function testConfig(overrides: Partial<DeployConfig> = {}): DeployConfig {
  return {
    region: 'us-east-1',
    defaultModelId: 'anthropic.claude-3-5-sonnet-20241022-v2:0',
    routerModelId: 'anthropic.claude-3-haiku-20240307-v1:0',
    blockedModelIds: [],
    allowedEmailDomains: ['example.com'],
    adminEmail: 'admin@example.com',
    limits: DEFAULT_LIMITS,
    webSearchEnabled: false,
    accountDeletionEnabled: false,
    allowLocalhostCallback: false,
    ...overrides,
  };
}

function build(config?: DeployConfig): { stack: BackendStack; template: Template } {
  const app = new cdk.App();
  const stack = new BackendStack(app, 'TestStack', {
    env: { region: 'us-east-1', account: '123456789012' },
    frontendAssetPath: FIXTURE_ASSET_PATH,
    config,
    webAclArn: config ? 'arn:aws:wafv2:us-east-1:123456789012:global/webacl/edge/abc123' : undefined,
  });
  return { stack, template: Template.fromStack(stack) };
}

function resourcesOf(template: Template, type: string): any[] {
  return Object.values(template.findResources(type)).map((r: any) => r.Properties);
}

function logicalIdOf(template: Template, type: string, match: (props: any) => boolean): string {
  const [id] = Object.entries(template.findResources(type)).find(([, r]: [string, any]) => match(r.Properties)) ?? [];
  if (!id) throw new Error(`no ${type} matched`);
  return id;
}

describe('ToolGateway (F4)', () => {
  const { template } = build(testConfig());

  describe('Cognito scopes and the Router client', () => {
    it('adds the cei-tools resource server with the 10 source scopes, reserved ncbi, and common (B69/B70/B86)', () => {
      const [server] = resourcesOf(template, 'AWS::Cognito::UserPoolResourceServer');
      expect(server.Identifier).toBe('cei-tools');
      expect(server.Scopes.map((s: any) => s.ScopeName).sort()).toEqual([...EXPECTED_SCOPES].sort());
    });

    it('scope names never contain one another (B69 substring rule)', () => {
      expect(() => assertNoScopeCollisions(EXPECTED_SCOPES)).not.toThrow();
      expect(() => assertNoScopeCollisions(['src.chem', 'src.chembl'])).toThrow(/src.chem in src.chembl/);
    });

    it('creates the Router M2M client: secret, client credentials only, every tool scope, 1 h tokens', () => {
      const routers = resourcesOf(template, 'AWS::Cognito::UserPoolClient').filter((c) => c.AllowedOAuthFlows?.includes('client_credentials'));
      expect(routers).toHaveLength(1);
      const [router] = routers;
      expect(router.GenerateSecret).toBe(true);
      expect(router.AllowedOAuthFlows).toEqual(['client_credentials']);
      expect(router.AccessTokenValidity).toBe(60);
      expect(router.ExplicitAuthFlows).toEqual(['ALLOW_REFRESH_TOKEN_AUTH']);
      expect(router.AllowedOAuthScopes).toHaveLength(EXPECTED_SCOPES.length);
      for (const scope of EXPECTED_SCOPES) expect(JSON.stringify(router.AllowedOAuthScopes)).toContain(scope);
    });

    it('keeps the SPA client off the client-credentials grant (the pre-token trigger relies on it)', () => {
      const spa = resourcesOf(template, 'AWS::Cognito::UserPoolClient').filter((c) => c.GenerateSecret === false);
      expect(spa).toHaveLength(1);
      expect(spa[0].AllowedOAuthFlows).toEqual(['code']);
    });

    it('attaches the pre-token-generation trigger with event version V3_0 (M2M customization)', () => {
      template.hasResourceProperties('AWS::Cognito::UserPool', {
        LambdaConfig: Match.objectLike({
          PreSignUp: Match.anyValue(),
          PreTokenGenerationConfig: { LambdaArn: Match.anyValue(), LambdaVersion: 'V3_0' },
        }),
      });
    });
  });

  describe('Router M2M secret (B30, S2 question 3)', () => {
    it('denies GetSecretValue to every principal except the Router and token-refresh roles', () => {
      const [policy] = resourcesOf(template, 'AWS::SecretsManager::ResourcePolicy');
      const deny = policy.ResourcePolicy.Statement.find((s: any) => s.Sid === 'DenyAllButRouterAndRefresh');
      expect(deny.Effect).toBe('Deny');
      expect(deny.Principal).toEqual({ AWS: '*' });
      expect(deny.Action).toBe('secretsmanager:GetSecretValue');
      const allowed = deny.Condition.StringNotEquals['aws:PrincipalArn'];
      expect(allowed).toHaveLength(2);
      const routerRole = logicalIdOf(template, 'AWS::IAM::Role', (p) => String(p.Description ?? '').startsWith('Session Router Lambda role'));
      expect(JSON.stringify(allowed)).toContain(routerRole);
    });
  });

  describe('Gateway, targets and Cedar policies', () => {
    it('uses a custom JWT authorizer on the main user pool, allowing only the Router client', () => {
      const [gateway] = resourcesOf(template, 'AWS::BedrockAgentCore::Gateway');
      expect(gateway.ProtocolType).toBe('MCP');
      expect(gateway.AuthorizerType).toBe('CUSTOM_JWT');
      const jwt = gateway.AuthorizerConfiguration.CustomJWTAuthorizer;
      expect(jwt.AllowedClients).toHaveLength(1);
      expect(JSON.stringify(jwt.DiscoveryUrl)).toContain('openid-configuration');
      expect(gateway.ExceptionLevel).toBeUndefined();
    });

    it('starts in LOG_ONLY by default and follows a configured ENFORCE (B30)', () => {
      expect(resourcesOf(template, 'AWS::BedrockAgentCore::Gateway')[0].PolicyEngineConfiguration.Mode).toBe('LOG_ONLY');
      expect(resourcesOf(build().template, 'AWS::BedrockAgentCore::Gateway')[0].PolicyEngineConfiguration.Mode).toBe('LOG_ONLY');
      const enforce = build(testConfig({ toolPolicyMode: 'ENFORCE' })).template;
      expect(resourcesOf(enforce, 'AWS::BedrockAgentCore::Gateway')[0].PolicyEngineConfiguration.Mode).toBe('ENFORCE');
    });

    it('has one Lambda target per source, each serving exactly the generated `search` tool', () => {
      const targets = resourcesOf(template, 'AWS::BedrockAgentCore::GatewayTarget').filter((target) => target.Name !== 'lsops' && !target.Name.startsWith('ncbi-'));
      expect(targets.map((t) => t.Name).sort()).toEqual([...SOURCE_IDS].sort());
      const schemas = new Map(loadToolSchemas().map((s) => [s.sourceId, s.tool]));
      for (const target of targets) {
        const [tool] = target.TargetConfiguration.Mcp.Lambda.ToolSchema.InlinePayload;
        expect(tool.Name).toBe('search');
        expect(tool.InputSchema.Properties).toBeDefined();
        expect(Object.keys(tool.InputSchema.Properties).sort()).toEqual(Object.keys((schemas.get(target.Name) as any).inputSchema.properties).sort());
        expect(target.CredentialProviderConfigurations).toEqual([{ CredentialProviderType: 'GATEWAY_IAM_ROLE' }]);
      }
    });

    it('writes one scoped Cedar permit per source target (B69)', () => {
      const policies = resourcesOf(template, 'AWS::BedrockAgentCore::Policy').filter((policy) => policy.Name !== 'permit_lsops' && !policy.Name.startsWith('permit_ncbi_'));
      expect(policies.map((p) => p.Name).sort()).toEqual(SOURCE_IDS.map((id) => `permit_${id}`).sort());
      for (const policy of policies) {
        const id = policy.Name.replace('permit_', '');
        const statement = JSON.stringify(policy.Definition.Cedar.Statement);
        expect(statement).toContain(`action in AgentCore::Action::\\"${id}\\"`);
        expect(statement).toContain(`like \\"*cei-tools/src.${id}*\\"`);
        expect(statement).not.toMatch(/forbid/);
      }
    });

    it('requires the common scope for LS Ops', () => {
      expect(cedarPermit('lsops', 'arn:gw')).toBe(
        'permit(principal is AgentCore::OAuthUser, action in AgentCore::Action::"lsops", resource == AgentCore::Gateway::"arn:gw");',
      );
      expect(cedarPermit('pubmed', 'arn:gw', 'cei-tools/src.pubmed')).toContain('principal.hasTag("scope") && principal.getTag("scope") like "*cei-tools/src.pubmed*"');
      const policy = resourcesOf(template, 'AWS::BedrockAgentCore::Policy').find((p) => p.Name === 'permit_lsops');
      expect(JSON.stringify(policy.Definition.Cedar.Statement)).toContain('cei-tools/common');
      const target = resourcesOf(template, 'AWS::BedrockAgentCore::GatewayTarget').find((t) => t.Name === 'lsops');
      expect(target.TargetConfiguration.Mcp.Lambda.ToolSchema.InlinePayload).toHaveLength(20);
    });

    it('scopes NCBI lookups and uses a shared rate table for PubMed', () => {
      const targets = resourcesOf(template, 'AWS::BedrockAgentCore::GatewayTarget').filter((t) => t.Name.startsWith('ncbi-'));
      expect(targets.map((t) => [t.Name, t.TargetConfiguration.Mcp.Lambda.ToolSchema.InlinePayload[0].Name]).sort())
        .toEqual([['ncbi-sequence', 'efetch'], ['ncbi-taxonomy', 'lineage']]);
      const policies = resourcesOf(template, 'AWS::BedrockAgentCore::Policy').filter((p) => p.Name.startsWith('permit_ncbi_'));
      expect(policies).toHaveLength(2);
      for (const policy of policies) expect(JSON.stringify(policy.Definition.Cedar.Statement)).toContain('cei-tools/src.ncbi');
      const functions = resourcesOf(template, 'AWS::Lambda::Function');
      const withLimiter = functions.filter((f) => f.Environment?.Variables?.NCBI_RATE_LIMIT_TABLE);
      expect(withLimiter).toHaveLength(2);
      expect(withLimiter[0].Environment.Variables.NCBI_RATE_LIMIT_TABLE).toEqual(withLimiter[1].Environment.Variables.NCBI_RATE_LIMIT_TABLE);
    });

    it('lets the Gateway role invoke exactly the configured source Lambdas', () => {
      // the Gateway's own RoleArn (the Code Interpreter role also trusts bedrock-agentcore)
      const gatewayRole = resourcesOf(template, 'AWS::BedrockAgentCore::Gateway')[0].RoleArn['Fn::GetAtt'][0];
      const statements = resourcesOf(template, 'AWS::IAM::Policy')
        .filter((p) => p.Roles.some((r: any) => r.Ref === gatewayRole))
        .flatMap((p) => p.PolicyDocument.Statement);
      const invokes = statements.filter((s: any) => s.Action === 'lambda:InvokeFunction');
      const targets = resourcesOf(template, 'AWS::BedrockAgentCore::GatewayTarget');
      const expected = [...new Set(targets.map((target) => JSON.stringify(target.TargetConfiguration.Mcp.Lambda.LambdaArn)))].sort();
      const resources = invokes.flatMap((invoke: any) => Array.isArray(invoke.Resource) ? invoke.Resource : [invoke.Resource]);
      expect(resources.map((arn: unknown) => JSON.stringify(arn)).sort()).toEqual(expected);
    });
  });

  describe('Web Search connector (B44/B45/B86)', () => {
    it('is absent when webSearchEnabled is false (the default)', () => {
      const targets = resourcesOf(template, 'AWS::BedrockAgentCore::GatewayTarget').filter((t) => t.Name === 'web-search');
      expect(targets).toHaveLength(0);
      const policies = resourcesOf(template, 'AWS::BedrockAgentCore::Policy').filter((p) => p.Name === 'permit_web_search');
      expect(policies).toHaveLength(0);
      const gatewayRole = resourcesOf(template, 'AWS::BedrockAgentCore::Gateway')[0].RoleArn['Fn::GetAtt'][0];
      const statements = resourcesOf(template, 'AWS::IAM::Policy')
        .filter((p) => p.Roles.some((r: any) => r.Ref === gatewayRole))
        .flatMap((p) => p.PolicyDocument.Statement);
      const actions = statements.flatMap((s: any) => (Array.isArray(s.Action) ? s.Action : [s.Action]));
      expect(actions).not.toContain('bedrock-agentcore:InvokeWebSearch');
    });

    it('adds an unpinned ConnectorSource target, common-scope permit, and InvokeWebSearch grant when enabled', () => {
      const { template: enabledTemplate } = build(testConfig({ webSearchEnabled: true }));

      const [target] = resourcesOf(enabledTemplate, 'AWS::BedrockAgentCore::GatewayTarget').filter((t) => t.Name === 'web-search');
      expect(target).toBeDefined();
      expect(target.TargetConfiguration.Mcp.Connector.Source).toEqual({ ConnectorId: 'web-search' });
      expect(target.CredentialProviderConfigurations).toEqual([{ CredentialProviderType: 'GATEWAY_IAM_ROLE' }]);

      const [policy] = resourcesOf(enabledTemplate, 'AWS::BedrockAgentCore::Policy').filter((p) => p.Name === 'permit_web_search');
      expect(policy).toBeDefined();
      const statement = JSON.stringify(policy.Definition.Cedar.Statement);
      expect(statement).toContain('action in AgentCore::Action::\\"web-search\\"');
      expect(statement).toContain('cei-tools/common');

      const gatewayRole = resourcesOf(enabledTemplate, 'AWS::BedrockAgentCore::Gateway')[0].RoleArn['Fn::GetAtt'][0];
      const statements = resourcesOf(enabledTemplate, 'AWS::IAM::Policy')
        .filter((p) => p.Roles.some((r: any) => r.Ref === gatewayRole))
        .flatMap((p) => p.PolicyDocument.Statement);
      const actions = statements.flatMap((s: any) => (Array.isArray(s.Action) ? s.Action : [s.Action]));
      expect(actions).toContain('bedrock-agentcore:InvokeWebSearch');
    });

    it('sets CEI_WEB_SEARCH_ENABLED on the agent runtimes to match the config value', () => {
      const disabled = resourcesOf(template, 'AWS::BedrockAgentCore::Runtime');
      for (const runtime of disabled) expect(runtime.EnvironmentVariables.CEI_WEB_SEARCH_ENABLED).toBe('false');

      const { template: enabledTemplate } = build(testConfig({ webSearchEnabled: true }));
      const enabled = resourcesOf(enabledTemplate, 'AWS::BedrockAgentCore::Runtime');
      expect(enabled.length).toBeGreaterThan(0);
      for (const runtime of enabled) expect(runtime.EnvironmentVariables.CEI_WEB_SEARCH_ENABLED).toBe('true');
    });

    it('passes cdk-nag with Web Search enabled', () => {
      const { stack } = build(testConfig({ webSearchEnabled: true, toolPolicyMode: 'ENFORCE' }));
      const errors = Annotations.fromStack(stack).findError('*', Match.stringLikeRegexp('AwsSolutions-.*'));
      expect(errors).toHaveLength(0);
    });
  });

  describe('source Lambdas', () => {
    const sources = resourcesOf(template, 'AWS::Lambda::Function').filter((f) => f.Environment?.Variables?.CEI_API_KEY_SECRET_PREFIX);

    it('uses Python 3.14 with bounded source timeouts and no direct-mode switch', () => {
      expect(sources).toHaveLength(SOURCE_IDS.length);
      for (const fn of sources) {
        expect(fn.Environment.Variables.CEI_DIRECT_MODE).toBeUndefined();
        expect(fn.Runtime).toBe('python3.14');
        const role = fn.Role['Fn::GetAtt'][0] as string;
        expect(fn.Timeout).toBe(role.toLowerCase().includes('openfda') ? 150 : 90);
        expect(fn.Layers).toHaveLength(1);
      }
    });

    it('grants the source roles no Secrets Manager access until F18 (B99)', () => {
      const sourceRoles = new Set(sources.map((fn) => fn.Role['Fn::GetAtt'][0]));
      const statements = resourcesOf(template, 'AWS::IAM::Policy')
        .filter((p) => p.Roles.some((r: any) => sourceRoles.has(r.Ref)))
        .flatMap((p) => p.PolicyDocument.Statement);
      expect(JSON.stringify(statements)).not.toContain('secretsmanager');
    });
  });

  describe('token refresh (B67)', () => {
    it('includes NCBI in the specialist Router grant', () => {
      const routers = resourcesOf(template, 'AWS::Lambda::Function').filter((f) => f.Environment?.Variables?.TOOL_SCOPES);
      expect(routers).toHaveLength(1);
      expect(routers[0].Environment.Variables.TOOL_SCOPES.split(',').sort())
        .toEqual(EXPECTED_SCOPES.map((scope) => `cei-tools/${scope}`).sort());
    });

    it('is POST /internal/token-refresh with AWS_IAM auth', () => {
      const iam = resourcesOf(template, 'AWS::ApiGateway::Method').filter((m) => m.AuthorizationType === 'AWS_IAM');
      expect(iam).toHaveLength(1);
      expect(iam[0].HttpMethod).toBe('POST');
      template.hasResourceProperties('AWS::ApiGateway::Resource', { PathPart: 'token-refresh' });
    });

    it('restricts refresh to the Research runtime and the configured source scopes', () => {
      const matches = resourcesOf(template, 'AWS::Lambda::Function').filter((f) => f.Environment?.Variables?.ALLOWED_CALLER_ROLE_ARNS !== undefined);
      expect(matches).toHaveLength(1);
      const env = matches[0].Environment.Variables;
      const research = resourcesOf(template, 'AWS::BedrockAgentCore::Runtime').find((r) => r.AgentRuntimeName === 'CEIResearchAgent');
      expect(env.ALLOWED_CALLER_ROLE_ARNS).toEqual(research.RoleArn);
      expect(env.ALLOWED_SCOPES.split(',').sort()).toEqual(EXPECTED_SCOPES.map((scope) => `cei-tools/${scope}`).sort());
      expect(env.ACTIVE_RUNS_TABLE).not.toBe('');
      expect(env.METADATA_TABLE).not.toBe('');
    });
  });

  it('passes cdk-nag with the cdk.json feature flags the CDK CLI applies (they turn ARN tokens into literals)', () => {
    // Regression: a suppression regex that only matched `<AWS::Partition>` passed here without context
    // but failed the real `cdk deploy` in CodeBuild, where cdk.json resolves the partition to `aws`.
    const context = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'cdk.json'), 'utf-8')).context;
    const app = new cdk.App({ context });
    const stack = new BackendStack(app, 'TestStack', {
      env: { region: 'us-east-2', account: '123456789012' },
      frontendAssetPath: FIXTURE_ASSET_PATH,
      config: testConfig({ region: 'us-east-2' }),
      webAclArn: 'arn:aws:wafv2:us-east-1:123456789012:global/webacl/edge/abc123',
    });
    const errors = Annotations.fromStack(stack).findError('*', Match.stringLikeRegexp('AwsSolutions-.*'));
    expect(errors).toHaveLength(0);
  });

  it('passes cdk-nag with ADR-format suppressions only', () => {
    const { stack } = build(testConfig({ toolPolicyMode: 'ENFORCE' }));
    const errors = Annotations.fromStack(stack).findError('*', Match.stringLikeRegexp('AwsSolutions-.*'));
    expect(errors).toHaveLength(0);
  });
});
