import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Annotations, Match, Template } from 'aws-cdk-lib/assertions';
import { BackendStack } from '../lib/backend-stack';
import { DeployConfig, DEFAULT_LIMITS } from '../lib/config/deploy-config';
import { JOB_STATUS_INDEX } from '../lib/constructs/background-jobs';

const FIXTURE_ASSET_PATH = path.join(__dirname, 'fixtures', 'spa');

const config: DeployConfig = {
  region: 'us-east-1',
  defaultModelId: 'anthropic.claude-3-5-sonnet-20241022-v2:0',
  routerModelId: 'anthropic.claude-3-haiku-20240307-v1:0',
  blockedModelIds: [],
  allowedEmailDomains: ['example.com'],
  adminEmail: 'admin@example.com',
  limits: DEFAULT_LIMITS,
  webSearchEnabled: true,
  allowLocalhostCallback: false,
};

const app = new cdk.App();
const stack = new BackendStack(app, 'JobsTestStack', {
  env: { region: 'us-east-1', account: '123456789012' },
  frontendAssetPath: FIXTURE_ASSET_PATH,
  config,
  webAclArn: 'arn:aws:wafv2:us-east-1:123456789012:global/webacl/edge/abc123',
});
const template = Template.fromStack(stack);

function statementsForRole(prefix: string): Array<{ Action: string | string[]; Resource: unknown }> {
  return Object.values(template.findResources('AWS::IAM::Policy'))
    .filter((p: any) => (p.Properties.Roles as Array<{ Ref: string }>).some((r) => r.Ref.startsWith(prefix)))
    .flatMap((p: any) => p.Properties.PolicyDocument.Statement);
}

function actionsForRole(prefix: string): string[] {
  return statementsForRole(prefix).flatMap((s) => ([] as string[]).concat(s.Action));
}

describe('F16 background jobs', () => {
  test('Job ID Store keys and the KEYS_ONLY status index match shared/persistence/jobs.py', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      KeySchema: [{ AttributeName: 'user_id', KeyType: 'HASH' }, { AttributeName: 'job_id', KeyType: 'RANGE' }],
      GlobalSecondaryIndexes: [{
        IndexName: JOB_STATUS_INDEX,
        KeySchema: [{ AttributeName: 'status', KeyType: 'HASH' }, { AttributeName: 'updated_at', KeyType: 'RANGE' }],
        Projection: { ProjectionType: 'KEYS_ONLY' },
      }],
      PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
    });
  });

  test('jobs routes exist and require the Cognito authorizer', () => {
    const methods = Object.values(template.findResources('AWS::ApiGateway::Method'))
      .filter((m: any) => m.Properties.HttpMethod !== 'OPTIONS' && m.Properties.AuthorizationType === 'COGNITO_USER_POOLS');
    const paths = Object.values(template.findResources('AWS::ApiGateway::Resource')).map((r: any) => r.Properties.PathPart);
    expect(paths).toEqual(expect.arrayContaining(['jobs', '{jobId}', 'cancel']));
    expect(methods.length).toBeGreaterThan(0);
    for (const method of Object.values(template.findResources('AWS::ApiGateway::Method')) as any[]) {
      if (method.Properties.HttpMethod !== 'OPTIONS') {
        expect(method.Properties.AuthorizationType).not.toBe('NONE');
      }
    }
  });

  test('the jobs Lambda can read and cancel jobs but never create or delete them', () => {
    const actions = actionsForRole('BackgroundJobsJobsHandlerRole');
    expect(actions).toEqual(expect.arrayContaining(['dynamodb:GetItem', 'dynamodb:Query', 'dynamodb:UpdateItem']));
    expect(actions).not.toContain('dynamodb:PutItem');
    expect(actions).not.toContain('dynamodb:DeleteItem');
    expect(actions).not.toContain('dynamodb:Scan');
  });

  test('the watchdog queries only the status index and runs every five minutes', () => {
    const statements = statementsForRole('BackgroundJobsJobWatchdogRole');
    const query = statements.find((s) => ([] as string[]).concat(s.Action).includes('dynamodb:Query'));
    expect(JSON.stringify(query?.Resource)).toContain(`/index/${JOB_STATUS_INDEX}`);
    expect(actionsForRole('BackgroundJobsJobWatchdogRole')).not.toContain('dynamodb:Scan');
    template.hasResourceProperties('AWS::Events::Rule', { ScheduleExpression: 'rate(5 minutes)' });
  });

  test('watchdog resume stays off until a runtime can resume from a checkpoint', () => {
    template.hasResourceProperties('AWS::Lambda::Function', {
      Environment: { Variables: Match.objectLike({ CEI_WATCHDOG_RESUME_ENABLED: 'false' }) },
    });
  });

  test('cancel URLs are provided for every job agent type', () => {
    const fn = Object.values(template.findResources('AWS::Lambda::Function')).find((f: any) =>
      f.Properties.Environment?.Variables?.CEI_RUNTIME_STOP_URLS !== undefined) as any;
    const urls = JSON.stringify(fn.Properties.Environment.Variables.CEI_RUNTIME_STOP_URLS);
    for (const agent of ['research', 'drafting', 'analysis', 'pipeline_manager']) {
      expect(urls).toContain(agent);
    }
    expect(urls).toContain('stopruntimesession?qualifier=DEFAULT');
  });

  test('token refresh can read the Job ID Store', () => {
    const fn = Object.values(template.findResources('AWS::Lambda::Function')).find((f: any) =>
      f.Properties.Environment?.Variables?.ALLOWED_CALLER_ROLE_ARNS !== undefined) as any;
    expect(fn.Properties.Environment.Variables.JOB_TABLE).toBeDefined();
  });

  test('only the Pipeline runtime writes jobs, with exact table actions', () => {
    const runtimes = Object.values(template.findResources('AWS::BedrockAgentCore::Runtime')) as any[];
    const withJobs = runtimes.filter((r) => r.Properties.EnvironmentVariables?.CEI_JOB_TABLE !== undefined);
    expect(withJobs).toHaveLength(1);
    expect(withJobs[0].Properties.AgentRuntimeName).toBe('CEIPipelineManager');
    expect(withJobs[0].Properties.EnvironmentVariables.CEI_ACTIVE_RUNS_TABLE).toBeDefined();
    const statements = statementsForRole('AgentRuntimesPipeline').filter((s) =>
      JSON.stringify(s.Resource).includes('BackgroundJobsJobIdStore'));
    expect(statements.flatMap((s) => ([] as string[]).concat(s.Action)).sort())
      .toEqual(['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem']);
  });

  test('no unsuppressed cdk-nag errors', () => {
    const errors = Annotations.fromStack(stack).findError('*', Match.stringLikeRegexp('AwsSolutions-.*'));
    expect(errors).toEqual([]);
  });
});

describe('F13 POST /ui-actions', () => {
  test('streams through the Cognito authorizer so long resumes are not cut at 29 seconds', () => {
    const resources = template.findResources('AWS::ApiGateway::Resource');
    const id = Object.entries(resources).find(([, r]: [string, any]) => r.Properties.PathPart === 'ui-actions')![0];
    const post = Object.values(template.findResources('AWS::ApiGateway::Method')).find((m: any) =>
      m.Properties.HttpMethod === 'POST' && m.Properties.ResourceId?.Ref === id) as any;
    expect(post.Properties.AuthorizationType).toBe('COGNITO_USER_POOLS');
    expect(post.Properties.Integration.ResponseTransferMode).toBe('STREAM');
  });

  test('the handler can only read chat metadata; runtimes authenticate the forwarded user token', () => {
    const data = statementsForRole('UiActionsRole').filter((s) =>
      ([] as string[]).concat(s.Action).some((a) => a.startsWith('dynamodb:') || a.startsWith('bedrock')));
    expect(data.flatMap((s) => ([] as string[]).concat(s.Action))).toEqual(['dynamodb:GetItem']);
    expect(JSON.stringify(data[0].Resource)).toContain('ChatMetadataStore');
  });
});
