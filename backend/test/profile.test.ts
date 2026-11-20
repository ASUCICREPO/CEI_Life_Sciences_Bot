import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
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
    webSearchEnabled: false,
    accountDeletionEnabled: false,
    allowLocalhostCallback: false,
    ...overrides,
  };
}

function build(config?: DeployConfig): Template {
  const app = new cdk.App();
  const stack = new BackendStack(app, 'ProfileTestStack', {
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
])('B112 POST /profile (%s)', (_label, config) => {
  const template = build(config);

  it('creates exactly one POST /profile route behind the Cognito authorizer', () => {
    const resources = template.findResources('AWS::ApiGateway::Resource') as Record<string, any>;
    const profileResourceId = Object.entries(resources).find(([, r]) => r.Properties.PathPart === 'profile')?.[0];
    expect(profileResourceId).toBeDefined();
    const methods = Object.values(template.findResources('AWS::ApiGateway::Method')) as any[];
    const postProfile = methods.filter(
      (m) => m.Properties.HttpMethod === 'POST' && m.Properties.ResourceId?.Ref === profileResourceId,
    );
    expect(postProfile).toHaveLength(1);
    expect(postProfile[0].Properties.AuthorizationType).toBe('COGNITO_USER_POOLS');
  });

  it('creates exactly one Profile Lambda function', () => {
    const functionLogicalIds = Object.keys(template.findResources('AWS::Lambda::Function'));
    expect(functionLogicalIds.filter((id) => id.startsWith('ProfileFn'))).toHaveLength(1);
  });

  it('scopes the Profile Lambda role to exactly the four Cognito Admin*/ListUsers actions on the one user pool', () => {
    const policies = Object.values(template.findResources('AWS::IAM::Policy')) as any[];
    const profilePolicies = policies.filter((p) =>
      (p.Properties.Roles as Array<{ Ref: string }>).some((r) => r.Ref.startsWith('ProfileRole')));
    const statements = profilePolicies.flatMap((p) => p.Properties.PolicyDocument.Statement);
    const cognitoStatement = statements.find((s: any) =>
      ([] as string[]).concat(s.Action).includes('cognito-idp:AdminUpdateUserAttributes'));
    expect(cognitoStatement).toBeDefined();
    expect(([] as string[]).concat(cognitoStatement.Action).sort()).toEqual(
      [
        'cognito-idp:AdminDisableUser',
        'cognito-idp:AdminUpdateUserAttributes',
        'cognito-idp:AdminUserGlobalSignOut',
        'cognito-idp:ListUsers',
      ].sort(),
    );
    // Not a wildcard resource: a single Fn::GetAtt to the one user pool's Arn.
    expect(cognitoStatement.Resource).not.toBe('*');
    expect(JSON.stringify(cognitoStatement.Resource)).not.toContain('"*"');
  });

  it('the Profile Lambda is never a Cognito lambdaTrigger (no CloudFormation cycle)', () => {
    const pools = Object.values(template.findResources('AWS::Cognito::UserPool')) as any[];
    for (const pool of pools) {
      const lambdaConfig = JSON.stringify(pool.Properties.LambdaConfig ?? {});
      expect(lambdaConfig).not.toContain('ProfileFn');
    }
  });
});
