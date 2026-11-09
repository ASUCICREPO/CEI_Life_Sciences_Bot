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
    // F4/B67: POST /internal/token-refresh is the one machine route, AWS_IAM instead of Cognito (tool-gateway.test.ts)
    const iamMethods = methods.filter((m) => m.AuthorizationType === 'AWS_IAM');
    expect(iamMethods.map((m) => m.HttpMethod)).toEqual(['POST']);
    const real = methods.filter((m) => m.HttpMethod !== 'OPTIONS' && m.AuthorizationType !== 'AWS_IAM');
    expect(real.map((m) => m.HttpMethod).sort()).toEqual(['GET', 'GET', 'GET', 'GET', 'PATCH', 'POST', 'POST']);
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

  it('includes the streaming and trace routes without a DELETE route', () => {
    const methods = Object.values(template.findResources('AWS::ApiGateway::Method')).map((m: any) => m.Properties);
    expect(methods.map((method) => method.HttpMethod)).not.toContain('DELETE');
    template.resourceCountIs('AWS::ApiGateway::Resource', 10);
    const resources = template.findResources('AWS::ApiGateway::Resource');
    const paths = Object.values(resources).map((r: any) => r.Properties.PathPart);
    expect(paths.sort()).toEqual(['chat', 'history', 'internal', 'sessions', 'stream', 'token-refresh', 'trace', 'turns', '{messageId}', '{sessionId}']);
    const idFor = (part: string) => Object.entries(resources).find(([, resource]: [string, any]) => resource.Properties.PathPart === part)![0];
    const stream = resources[idFor('stream')] as any;
    const trace = resources[idFor('trace')] as any;
    expect(stream.Properties.ParentId).toEqual({ Ref: idFor('chat') });
    expect(trace.Properties.ParentId).toEqual({ Ref: idFor('{messageId}') });
    expect(methods.some((method) => method.HttpMethod === 'POST' && method.ResourceId?.Ref === idFor('stream') && method.Integration.ResponseTransferMode === 'STREAM')).toBe(true);
    expect(methods.some((method) => method.HttpMethod === 'GET' && method.ResourceId?.Ref === idFor('trace') && method.AuthorizationType === 'COGNITO_USER_POOLS')).toBe(true);
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

  it('gateway error responses carry CORS headers so the browser can read the envelope', () => {
    for (const type of ['UNAUTHORIZED', 'ACCESS_DENIED', 'THROTTLED', 'DEFAULT_4XX', 'DEFAULT_5XX']) {
      template.hasResourceProperties('AWS::ApiGateway::GatewayResponse', {
        ResponseType: type,
        ResponseParameters: Match.objectLike({
          'gatewayresponse.header.Access-Control-Allow-Origin': Match.anyValue(),
          'gatewayresponse.header.Access-Control-Allow-Headers': "'Authorization,Content-Type,Accept'",
          'gatewayresponse.header.Vary': "'Origin'",
        }),
      });
    }
  });

  it('CORS preflight allows the headers the SPA sends (Authorization, Content-Type, Accept)', () => {
    template.hasResourceProperties('AWS::ApiGateway::Method', {
      HttpMethod: 'OPTIONS',
      Integration: Match.objectLike({
        IntegrationResponses: Match.arrayWith([
          Match.objectLike({
            ResponseParameters: Match.objectLike({
              'method.response.header.Access-Control-Allow-Headers': Match.stringLikeRegexp('Authorization,Content-Type,Accept'),
            }),
          }),
        ]),
      }),
    });
  });

  it('creates the session tables and active-run table with the specified keys', () => {
    const tables = template.findResources('AWS::DynamoDB::Table');
    expect(Object.keys(tables)).toHaveLength(3);
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
      expect(table.DeletionPolicy).toBe('Retain');
    }
  });

  it('gives only ChatMetadataStore the sort and title-search GSIs (projection ALL)', () => {
    const tables = Object.values(template.findResources('AWS::DynamoDB::Table')) as any[];
    const metadata = tables.find((t) => t.Properties.KeySchema[0].AttributeName === 'user_id');
    const history = tables.find((t) => t.Properties.KeySchema[0].AttributeName === 'session_id');
    expect(history.Properties.GlobalSecondaryIndexes).toBeUndefined();
    const gsis = metadata.Properties.GlobalSecondaryIndexes;
    expect(gsis.map((g: any) => g.IndexName).sort()).toEqual(['by_activity', 'by_created', 'by_title', 'by_updated']);
    const sortKeys: Record<string, string> = {
      by_created: 'created_at',
      by_updated: 'updated_at',
      by_activity: 'last_activity_at',
      by_title: 'title_search',
    };
    for (const g of gsis) {
      expect(g.KeySchema[0]).toEqual({ AttributeName: 'user_id', KeyType: 'HASH' });
      expect(g.KeySchema[1].AttributeName).toBe(sortKeys[g.IndexName]);
      expect(g.Projection).toEqual({ ProjectionType: 'ALL' });
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
    expect([...new Set(metadataActions)].sort()).toEqual(['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:Query', 'dynamodb:UpdateItem']);

    const historyStatements = policyActionsFor(template, 'SessionHistoryHandlerRole');
    const dynamo = historyStatements.filter((s) => s.actions.some((a) => a.startsWith('dynamodb:')));
    expect(dynamo.flatMap((s) => s.actions).sort()).toEqual(['dynamodb:BatchGetItem', 'dynamodb:GetItem', 'dynamodb:GetItem', 'dynamodb:Query']);

    for (const statement of [...metadataStatements, ...historyStatements]) {
      const resource = JSON.stringify(statement.resource);
      expect(resource).not.toContain('"*"');
      expect(resource).not.toContain('/index/*');
      if (resource.includes('/index/')) {
        // only the metadata handler may Query, and only the named GSIs
        expect(statement.actions).toEqual(['dynamodb:Query']);
        expect(historyStatements).not.toContain(statement);
      }
      expect(statement.actions).not.toContain('dynamodb:DeleteItem');
      expect(statement.actions).not.toContain('dynamodb:Scan');
    }
  });

  it('lets the metadata handler read the history owner stamp (Query only) for its ownership check', () => {
    const statements = policyActionsFor(template, 'SessionMetadataHandlerRole');
    const onHistory = statements.filter((s) => JSON.stringify(s.resource).includes('ChatHistoryStore'));
    expect(onHistory.map((s) => s.actions.join(','))).toEqual(['dynamodb:Query']);
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
