import * as fs from 'fs';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { BackendStack } from '../lib/backend-stack';
import { DeployConfig, DEFAULT_LIMITS } from '../lib/config/deploy-config';

const FIXTURE_ASSET_PATH = path.join(__dirname, 'fixtures', 'spa');

function testConfig(): DeployConfig {
  return {
    region: 'us-east-1',
    defaultModelId: 'anthropic.claude-3-5-sonnet-20241022-v2:0',
    routerModelId: 'anthropic.claude-3-haiku-20240307-v1:0',
    blockedModelIds: [],
    allowedEmailDomains: ['example.com'],
    adminEmail: 'admin@example.com',
    limits: DEFAULT_LIMITS,
    webSearchEnabled: true,
    accountDeletionEnabled: false,
    allowLocalhostCallback: false,
  };
}

function build(config?: DeployConfig): Template {
  const app = new cdk.App();
  const stack = new BackendStack(app, 'AuthTestStack', {
    env: { region: 'us-east-1', account: '123456789012' },
    frontendAssetPath: FIXTURE_ASSET_PATH,
    config,
    webAclArn: config ? 'arn:aws:wafv2:us-east-1:123456789012:global/webacl/edge/abc123' : undefined,
  });
  return Template.fromStack(stack);
}

describe.each([
  ['with deploy config', testConfig()],
  ['without deploy config', undefined],
])('Session API auth (%s)', (_label, config) => {
  const template = build(config);
  const methods = Object.entries(template.findResources('AWS::ApiGateway::Method'));
  // ADR: exempt only the F4/B67 machine route | Rationale: POST /internal/token-refresh is called by an agent runtime
  // role with SigV4, not a signed-in user, so it is AWS_IAM by design; the test below pins it to that one route
  const iamMethods = methods.filter(([, m]) => (m as any).Properties.AuthorizationType === 'AWS_IAM');
  const nonOptions = methods.filter(
    ([, m]) => (m as any).Properties.HttpMethod !== 'OPTIONS' && (m as any).Properties.AuthorizationType !== 'AWS_IAM',
  );

  test('the only AWS_IAM method is POST /internal/token-refresh (B67)', () => {
    expect(iamMethods).toHaveLength(1);
    const props = (iamMethods[0][1] as any).Properties;
    expect(props.HttpMethod).toBe('POST');
    const resource = template.findResources('AWS::ApiGateway::Resource')[props.ResourceId.Ref] as any;
    expect(resource.Properties.PathPart).toBe('token-refresh');
  });
  const options = methods.filter(([, m]) => (m as any).Properties.HttpMethod === 'OPTIONS');
  const authorizers = template.findResources('AWS::ApiGateway::Authorizer');
  const authorizerIds = Object.keys(authorizers);
  const userPoolIds = Object.keys(template.findResources('AWS::Cognito::UserPool'));

  test('the expected authenticated session, trace, and streaming routes exist', () => {
    const resources = template.findResources('AWS::ApiGateway::Resource') as Record<string, any>;
    function route(id: string): string {
      const resource = resources[id];
      const parentId = resource.Properties.ParentId?.Ref;
      return `${parentId && resources[parentId] ? route(parentId) : ''}/${resource.Properties.PathPart}`;
    }
    const routes = nonOptions.map(([, m]) => {
      const props = (m as any).Properties;
      return `${props.HttpMethod} ${route(props.ResourceId.Ref)}`;
    }).sort();
    expect(routes).toEqual([
      'GET /exports/{exportId}/download', 'GET /sessions', 'GET /sessions/{sessionId}', 'GET /sessions/{sessionId}/exports', 'GET /sessions/{sessionId}/history',
      'GET /sessions/{sessionId}/turns/{messageId}/trace', 'PATCH /sessions/{sessionId}',
      'POST /chat/stream', 'POST /client-diagnostics', 'POST /sessions',
      'GET /jobs', 'GET /jobs/{jobId}', 'POST /jobs/{jobId}/cancel', 'POST /ui-actions',
      'GET /models', 'GET /settings', 'PUT /settings',
    ].sort());
  });

  test('every non-OPTIONS method uses COGNITO_USER_POOLS with scopes and the stack authorizer', () => {
    expect(nonOptions.length).toBeGreaterThan(0);
    for (const [id, m] of nonOptions) {
      const props = (m as any).Properties;
      expect({ id, type: props.AuthorizationType }).toEqual({ id, type: 'COGNITO_USER_POOLS' });
      expect(Array.isArray(props.AuthorizationScopes) && props.AuthorizationScopes.length > 0).toBe(true);
      expect(authorizerIds).toContain(props.AuthorizerId?.Ref);
    }
  });

  test('only CORS preflight OPTIONS methods are unauthenticated, and they are MOCK integrations', () => {
    for (const [, m] of methods) {
      const props = (m as any).Properties;
      if (props.AuthorizationType === 'NONE' || props.AuthorizationType === undefined) {
        expect(props.HttpMethod).toBe('OPTIONS');
      }
    }
    for (const [, m] of options) {
      expect((m as any).Properties.Integration.Type).toBe('MOCK');
    }
  });

  test('method scopes are scopes the SPA app client is allowed to request', () => {
    const clients = Object.values(template.findResources('AWS::Cognito::UserPoolClient'));
    const allowed = new Set<string>(clients.flatMap((c) => (c as any).Properties.AllowedOAuthScopes ?? []));
    for (const [, m] of nonOptions) {
      for (const scope of (m as any).Properties.AuthorizationScopes) {
        expect(allowed.has(scope)).toBe(true);
      }
    }
  });

  test('the authorizer is a Cognito user-pool authorizer referencing the stack user pool', () => {
    expect(authorizerIds).toHaveLength(1);
    const props = (authorizers[authorizerIds[0]] as any).Properties;
    expect(props.Type).toBe('COGNITO_USER_POOLS');
    expect(props.IdentitySource).toBe('method.request.header.Authorization');
    expect(userPoolIds).toHaveLength(1);
    expect(props.ProviderARNs).toEqual([{ 'Fn::GetAtt': [userPoolIds[0], 'Arn'] }]);
  });
});

test('no CEI_DIRECT_MODE env var remains anywhere in backend/lib', () => {
  const libDir = path.join(__dirname, '..', 'lib');
  const offenders: string[] = [];
  const walk = (dir: string) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) walk(full);
      else if (/\.ts$/.test(entry.name) && fs.readFileSync(full, 'utf8').includes('CEI_DIRECT_MODE')) offenders.push(full);
    }
  };
  walk(libDir);
  // a comment may say "no CEI_DIRECT_MODE"; any other mention (e.g. an env var on a runtime) fails
  const bad = offenders.filter((f) => {
    const lines = fs.readFileSync(f, 'utf8').split(/\r?\n/).filter((l) => l.includes('CEI_DIRECT_MODE'));
    return lines.some((l) => !/^\s*\/\/.*no CEI_DIRECT_MODE/.test(l));
  });
  expect(bad).toEqual([]);
});
