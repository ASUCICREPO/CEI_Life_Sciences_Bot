import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Annotations, Match, Template } from 'aws-cdk-lib/assertions';
import { BackendStack } from '../lib/backend-stack';
import { DeployConfig, DEFAULT_LIMITS } from '../lib/config/deploy-config';

const FIXTURE_ASSET_PATH = path.join(__dirname, 'fixtures', 'spa');

function testConfig(overrides: Partial<DeployConfig> = {}): DeployConfig {
  return {
    region: 'us-east-1',
    defaultModelId: 'anthropic.claude-3-5-sonnet-20241022-v2:0',
    routerModelId: 'anthropic.claude-3-haiku-20240307-v1:0',
    blockedModelIds: [],
    allowedEmailDomains: ['example.com'],
    adminEmail: 'admin@example.com',
    limits: DEFAULT_LIMITS,
    webSearchEnabled: true,
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

function policyActionsFor(template: Template, roleLogicalIdPrefix: string): Array<{ actions: string[]; resource: unknown }> {
  const policies = template.findResources('AWS::IAM::Policy');
  const found: Array<{ actions: string[]; resource: unknown }> = [];
  for (const policy of Object.values(policies)) {
    const roles = (policy as any).Properties.Roles as Array<{ Ref: string }>;
    if (!roles.some((r) => r.Ref.startsWith(roleLogicalIdPrefix))) continue;
    for (const statement of (policy as any).Properties.PolicyDocument.Statement) {
      found.push({
        actions: ([] as string[]).concat(statement.Action),
        resource: statement.Resource,
      });
    }
  }
  return found;
}

describe('BackendStack REST API + session store', () => {
  const { template } = build(testConfig());

  it('creates one Regional REST API', () => {
    template.resourceCountIs('AWS::ApiGateway::RestApi', 1);
    template.hasResourceProperties('AWS::ApiGateway::RestApi', {
      EndpointConfiguration: { Types: ['REGIONAL'] },
    });
  });

  it('puts the Cognito authorizer on every non-OPTIONS method', () => {
    template.resourceCountIs('AWS::ApiGateway::Authorizer', 1);
    template.hasResourceProperties('AWS::ApiGateway::Authorizer', { Type: 'COGNITO_USER_POOLS' });
    const methods = Object.values(template.findResources('AWS::ApiGateway::Method')).map((m: any) => m.Properties);
    const real = methods.filter((m) => m.HttpMethod !== 'OPTIONS');
    expect(real.map((m) => m.HttpMethod).sort()).toEqual(['GET', 'GET', 'GET', 'PATCH', 'POST']);
    for (const method of real) {
      expect(method.AuthorizationType).toBe('COGNITO_USER_POOLS');
      expect(method.AuthorizerId).toBeDefined();
      expect(method.AuthorizationScopes).toEqual(['openid']);
    }
    // ADR: preflight is the only unauthenticated method | browsers send no credentials on OPTIONS
    for (const method of methods.filter((m) => m.HttpMethod === 'OPTIONS')) {
      expect(method.AuthorizationType).toBe('NONE');
      expect(method.Integration.Type).toBe('MOCK');
    }
  });

  it('has no DELETE route and no streaming route yet', () => {
    const methods = Object.values(template.findResources('AWS::ApiGateway::Method')).map((m: any) => m.Properties.HttpMethod);
    expect(methods).not.toContain('DELETE');
    template.resourceCountIs('AWS::ApiGateway::Resource', 3);
  });

  it('applies the 20 rps account-wide throttle through a UsagePlan and the stage default', () => {
    template.hasResourceProperties('AWS::ApiGateway::UsagePlan', {
      Throttle: { RateLimit: 20, BurstLimit: 20 },
    });
    template.hasResourceProperties('AWS::ApiGateway::Stage', {
      MethodSettings: [Match.objectLike({ HttpMethod: '*', ResourcePath: '/*', ThrottlingRateLimit: 20, ThrottlingBurstLimit: 20 })],
    });
  });

  it('follows a configured requestsPerSecond', () => {
    const custom = build(testConfig({ limits: { ...DEFAULT_LIMITS, requestsPerSecond: 7 } }));
    custom.template.hasResourceProperties('AWS::ApiGateway::UsagePlan', {
      Throttle: { RateLimit: 7, BurstLimit: 7 },
    });
  });

  it('writes access logs', () => {
    template.hasResourceProperties('AWS::ApiGateway::Stage', {
      AccessLogSetting: Match.objectLike({ DestinationArn: Match.anyValue() }),
    });
  });

  it('maps gateway 401/403/429 and default 4XX/5XX to the error envelope', () => {
    const expected: Record<string, string> = {
      UNAUTHORIZED: 'unauthorized',
      ACCESS_DENIED: 'forbidden',
      THROTTLED: 'rate_limited',
      DEFAULT_4XX: 'invalid_request',
      DEFAULT_5XX: 'internal_error',
    };
    template.resourceCountIs('AWS::ApiGateway::GatewayResponse', Object.keys(expected).length);
    for (const [type, code] of Object.entries(expected)) {
      template.hasResourceProperties('AWS::ApiGateway::GatewayResponse', {
        ResponseType: type,
        ResponseTemplates: { 'application/json': Match.stringLikeRegexp(`"code":"${code}".*"request_id":"\\$context.requestId"`) },
      });
    }
    template.hasResourceProperties('AWS::ApiGateway::GatewayResponse', { ResponseType: 'UNAUTHORIZED', StatusCode: '401' });
    template.hasResourceProperties('AWS::ApiGateway::GatewayResponse', { ResponseType: 'THROTTLED', StatusCode: '429' });
  });

  it('creates both tables with the specified keys, encryption, PITR and no TTL or GSI', () => {
    const tables = template.findResources('AWS::DynamoDB::Table');
    expect(Object.keys(tables)).toHaveLength(2);
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      KeySchema: [
        { AttributeName: 'user_id', KeyType: 'HASH' },
        { AttributeName: 'session_id', KeyType: 'RANGE' },
      ],
    });
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      KeySchema: [
        { AttributeName: 'session_id', KeyType: 'HASH' },
        { AttributeName: 'sequence_number', KeyType: 'RANGE' },
      ],
      AttributeDefinitions: Match.arrayWith([{ AttributeName: 'sequence_number', AttributeType: 'N' }]),
    });
    for (const table of Object.values(tables) as any[]) {
      expect(table.Properties.BillingMode).toBe('PAY_PER_REQUEST');
      // ADR: SSEEnabled without SSEType | CDK's AWS_MANAGED emits exactly this; it selects the aws/dynamodb managed key, while omitting it means an AWS-owned key
      expect(table.Properties.SSESpecification).toEqual({ SSEEnabled: true });
      expect(table.Properties.PointInTimeRecoverySpecification).toEqual({ PointInTimeRecoveryEnabled: true });
      expect(table.Properties.TimeToLiveSpecification).toBeUndefined();
      expect(table.Properties.GlobalSecondaryIndexes).toBeUndefined();
      expect(table.DeletionPolicy).toBe('Retain');
    }
  });

  it('creates two Python 3.14 session Lambdas on one shared layer', () => {
    // ADR: match by description | BucketDeployment adds its own CDK-owned AWS CLI layer
    template.resourcePropertiesCountIs(
      'AWS::Lambda::LayerVersion',
      { Description: Match.stringLikeRegexp('shared persistence code') },
      1,
    );
    for (const handlerName of ['metadata_handler.handler', 'history_handler.handler']) {
      template.hasResourceProperties('AWS::Lambda::Function', {
        Handler: handlerName,
        Runtime: 'python3.14',
        Layers: [Match.anyValue()],
        Environment: {
          Variables: Match.objectLike({
            CEI_CHAT_METADATA_TABLE: Match.anyValue(),
            CEI_CHAT_HISTORY_TABLE: Match.anyValue(),
            CEI_AWS_REGION: 'us-east-1',
            ALLOWED_ORIGINS: Match.anyValue(),
          }),
        },
      });
    }
  });

  it('grants each handler only the table actions it needs', () => {
    const metadataStatements = policyActionsFor(template, 'SessionMetadataHandlerRole');
    const metadataActions = metadataStatements.flatMap((s) => s.actions).filter((a) => a.startsWith('dynamodb:'));
    expect(metadataActions.sort()).toEqual(['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:Query', 'dynamodb:UpdateItem']);

    const historyStatements = policyActionsFor(template, 'SessionHistoryHandlerRole');
    const dynamo = historyStatements.filter((s) => s.actions.some((a) => a.startsWith('dynamodb:')));
    expect(dynamo.map((s) => s.actions.join(',')).sort()).toEqual(['dynamodb:GetItem', 'dynamodb:Query']);

    for (const statement of [...metadataStatements, ...historyStatements]) {
      expect(JSON.stringify(statement.resource)).not.toContain('"*"');
      expect(JSON.stringify(statement.resource)).not.toContain('/index/');
      expect(statement.actions).not.toContain('dynamodb:DeleteItem');
      expect(statement.actions).not.toContain('dynamodb:Scan');
    }
  });

  it('exposes apiBaseUrl in config.json and as an output', () => {
    template.hasOutput('ApiBaseUrl', { Value: Match.anyValue() });
    // The config.json value is a token, so it shows up as a marker that resolves to the execute-api URL
    const deployments = Object.values(template.findResources('Custom::CDKBucketDeployment')) as any[];
    expect(deployments.some((d) => JSON.stringify(d.Properties.SourceMarkers ?? []).includes('execute-api'))).toBe(true);
  });

  it('never prunes config.json from the site bucket', () => {
    // Regression: a frontend-only change re-ran only the pruning SPA deployment, which deleted config.json
    const deployments = Object.values(template.findResources('Custom::CDKBucketDeployment')) as any[];
    const pruning = deployments.filter((d) => d.Properties.Prune !== false);
    expect(pruning.length).toBeGreaterThan(0);
    for (const d of pruning) {
      expect(d.Properties.Exclude).toContain('config.json');
    }
  });

  it('allows only the site origin for CORS without the localhost flag', () => {
    const json = JSON.stringify(template.toJSON());
    expect(json).not.toContain('http://localhost:5173');
    expect(json).not.toMatch(/Access-Control-Allow-Origin[^}]*'\*'/);
  });

  it('adds the localhost origin only when allowLocalhostCallback is set', () => {
    const local = build(testConfig({ allowLocalhostCallback: true }));
    local.template.hasResourceProperties('AWS::Lambda::Function', {
      Environment: { Variables: Match.objectLike({ ALLOWED_ORIGINS: Match.objectLike({ 'Fn::Join': Match.anyValue() }) }) },
    });
    expect(JSON.stringify(local.template.toJSON())).toContain('http://localhost:5173');
  });

  it('lets the SPA call the API origin in its CSP', () => {
    template.hasResourceProperties('AWS::CloudFront::ResponseHeadersPolicy', {
      ResponseHeadersPolicyConfig: {
        SecurityHeadersConfig: {
          ContentSecurityPolicy: { ContentSecurityPolicy: Match.objectLike({ 'Fn::Join': Match.anyValue() }) },
        },
      },
    });
    expect(JSON.stringify(template.toJSON())).toContain('execute-api');
  });

  it('passes cdk-nag with and without a config', () => {
    for (const { stack } of [build(), build(testConfig())]) {
      const errors = Annotations.fromStack(stack).findError('*', Match.stringLikeRegexp('AwsSolutions-.*'));
      expect(errors).toHaveLength(0);
    }
  });

  it('uses only ADR-format cdk-nag suppression reasons', () => {
    const json = JSON.stringify(build(testConfig()).template.toJSON());
    const reasons = [...json.matchAll(/"reason":"((?:[^"\\]|\\.)*)"/g)].map((m) => m[1]);
    expect(reasons.length).toBeGreaterThan(0);
    for (const encoded of reasons) {
      // cdk-nag stores some reasons base64-encoded in resource metadata
      const reason = encoded.startsWith('ADR: ') ? encoded : Buffer.from(encoded, 'base64').toString('utf8');
      expect(reason).toMatch(/^ADR: .+\| Rationale: .+/);
    }
  });
});
