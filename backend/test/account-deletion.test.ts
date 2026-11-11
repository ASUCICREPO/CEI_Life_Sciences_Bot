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
    webSearchEnabled: false,
    accountDeletionEnabled: false,
    allowLocalhostCallback: false,
    ...overrides,
  };
}

function build(config: DeployConfig): { stack: BackendStack; template: Template } {
  const app = new cdk.App();
  const stack = new BackendStack(app, 'TestStack', {
    env: { region: 'us-east-1', account: '123456789012' },
    frontendAssetPath: FIXTURE_ASSET_PATH,
    config,
    webAclArn: 'arn:aws:wafv2:us-east-1:123456789012:global/webacl/edge/abc123',
  });
  return { stack, template: Template.fromStack(stack) };
}

function methodList(template: Template): any[] {
  return Object.values(template.findResources('AWS::ApiGateway::Method')).map((m: any) => m.Properties);
}

describe('F19/B109 account deletion', () => {
  describe('accountDeletionEnabled: false (default)', () => {
    const { stack, template } = build(testConfig());

    it('creates no account-delete Lambdas and no /account route', () => {
      const functionLogicalIds = Object.keys(template.findResources('AWS::Lambda::Function'));
      expect(functionLogicalIds.some((id) => id.startsWith('AccountDeletion'))).toBe(false);
      const paths = Object.values(template.findResources('AWS::ApiGateway::Resource')).map((r: any) => r.Properties.PathPart);
      expect(paths).not.toContain('account');
      expect(paths).not.toContain('delete');
    });

    it('creates no Scheduler schedule group', () => {
      template.resourceCountIs('AWS::Scheduler::ScheduleGroup', 0);
    });

    it('never creates a DELETE /sessions/{sessionId} route (B104)', () => {
      expect(methodList(template).map((m) => m.HttpMethod)).not.toContain('DELETE');
    });

    it('ships accountDeletionEnabled: false in the SPA runtime config', () => {
      expect(stack.runtimeConfig.accountDeletionEnabled).toBe(false);
    });

    it('produces zero unsuppressed cdk-nag AwsSolutions errors', () => {
      const errors = Annotations.fromStack(stack).findError('*', Match.stringLikeRegexp('AwsSolutions-.*'));
      expect(errors).toHaveLength(0);
    });
  });

  describe('accountDeletionEnabled: true', () => {
    const { stack, template } = build(testConfig({ accountDeletionEnabled: true }));

    it('creates the account-delete Lambda, the delete-worker Lambda, and the POST /account/delete route', () => {
      const functionLogicalIds = Object.keys(template.findResources('AWS::Lambda::Function'));
      expect(functionLogicalIds.some((id) => id.startsWith('AccountDeletionDeleteFn'))).toBe(true);
      expect(functionLogicalIds.some((id) => id.startsWith('AccountDeletionWorkerFn'))).toBe(true);

      const resources = template.findResources('AWS::ApiGateway::Resource');
      const idFor = (part: string) => Object.entries(resources).find(([, r]: [string, any]) => r.Properties.PathPart === part)![0];
      const deleteResourceId = idFor('delete');
      const accountResourceId = idFor('account');
      expect((resources[deleteResourceId] as any).Properties.ParentId).toEqual({ Ref: accountResourceId });

      const methods = methodList(template);
      const postDelete = methods.find((m) => m.HttpMethod === 'POST' && m.ResourceId?.Ref === deleteResourceId);
      expect(postDelete).toBeDefined();
      expect(postDelete.AuthorizationType).toBe('COGNITO_USER_POOLS');
    });

    it('creates exactly one dedicated Scheduler schedule group', () => {
      template.resourceCountIs('AWS::Scheduler::ScheduleGroup', 1);
      template.hasResourceProperties('AWS::Scheduler::ScheduleGroup', { Name: 'cei-account-delete' });
    });

    it('never creates a DELETE /sessions/{sessionId} route even with the flag on (B104)', () => {
      expect(methodList(template).map((m) => m.HttpMethod)).not.toContain('DELETE');
    });

    it('ships accountDeletionEnabled: true in the SPA runtime config', () => {
      expect(stack.runtimeConfig.accountDeletionEnabled).toBe(true);
    });

    it('scopes the account-delete Lambda role to Cognito admin actions on the one user pool only', () => {
      const policies = Object.values(template.findResources('AWS::IAM::Policy')) as any[];
      const deleteRolePolicies = policies.filter((p) =>
        (p.Properties.Roles as Array<{ Ref: string }>).some((r) => r.Ref.startsWith('AccountDeletionDeleteRole')));
      const statements = deleteRolePolicies.flatMap((p) => p.Properties.PolicyDocument.Statement);
      const cognitoStatement = statements.find((s: any) =>
        ([] as string[]).concat(s.Action).includes('cognito-idp:AdminDisableUser'));
      expect(cognitoStatement).toBeDefined();
      expect(([] as string[]).concat(cognitoStatement.Action).sort()).toEqual(
        ['cognito-idp:AdminDisableUser', 'cognito-idp:AdminUserGlobalSignOut'].sort(),
      );
      // Not a wildcard resource: a single Fn::GetAtt to the one user pool's Arn.
      expect(cognitoStatement.Resource).not.toBe('*');
    });

    it('scopes the delete-worker role to AdminDeleteUser and the one AgentCore Memory ARN only', () => {
      const policies = Object.values(template.findResources('AWS::IAM::Policy')) as any[];
      const workerPolicies = policies.filter((p) =>
        (p.Properties.Roles as Array<{ Ref: string }>).some((r) => r.Ref.startsWith('AccountDeletionWorkerRole')));
      const statements = workerPolicies.flatMap((p) => p.Properties.PolicyDocument.Statement);
      const cognitoStatement = statements.find((s: any) =>
        ([] as string[]).concat(s.Action).includes('cognito-idp:AdminDeleteUser'));
      expect(cognitoStatement).toBeDefined();
      expect(cognitoStatement.Resource).not.toBe('*');
      const memoryStatement = statements.find((s: any) =>
        ([] as string[]).concat(s.Action).includes('bedrock-agentcore:BatchDeleteMemoryRecords'));
      expect(memoryStatement).toBeDefined();
      expect(memoryStatement.Resource).not.toBe('*');
    });

    it('scopes Secrets Manager delete to the cei/user-keys prefix, not every secret', () => {
      const policies = Object.values(template.findResources('AWS::IAM::Policy')) as any[];
      const workerPolicies = policies.filter((p) =>
        (p.Properties.Roles as Array<{ Ref: string }>).some((r) => r.Ref.startsWith('AccountDeletionWorkerRole')));
      const statements = workerPolicies.flatMap((p) => p.Properties.PolicyDocument.Statement);
      const secretsStatement = statements.find((s: any) =>
        ([] as string[]).concat(s.Action).includes('secretsmanager:DeleteSecret'));
      expect(secretsStatement).toBeDefined();
      const resource = ([] as string[]).concat(secretsStatement.Resource)[0];
      expect(typeof resource === 'string' ? resource : JSON.stringify(resource)).toMatch(/cei\/user-keys/);
    });

    it('produces zero unsuppressed cdk-nag AwsSolutions errors', () => {
      const errors = Annotations.fromStack(stack).findError('*', Match.stringLikeRegexp('AwsSolutions-.*'));
      expect(errors).toHaveLength(0);
    });
  });
});
