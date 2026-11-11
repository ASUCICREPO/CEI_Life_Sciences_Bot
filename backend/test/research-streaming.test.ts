import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { BackendStack } from '../lib/backend-stack';

describe('Research streaming infrastructure', () => {
  const app = new cdk.App();
  const stack = new BackendStack(app, 'StreamingTest', {
    env: { account: '123456789012', region: 'us-east-1' },
    frontendAssetPath: path.join(__dirname, 'fixtures', 'spa'),
  });
  const template = Template.fromStack(stack);

  it('enables X-Ray on the API stage and every application Lambda', () => {
    template.hasResourceProperties('AWS::ApiGateway::Stage', { TracingEnabled: true });
    const functions = Object.entries(template.findResources('AWS::Lambda::Function'))
      .filter(([id]) => !id.startsWith('Custom') && !id.startsWith('BucketNotificationsHandler'));
    expect(functions.length).toBeGreaterThan(10);
    for (const [, resource] of functions) {
      expect((resource as any).Properties.TracingConfig).toEqual({ Mode: 'Active' });
    }
  });

  it('uses a Cognito-authorized REST streaming integration', () => {
    const resources = template.findResources('AWS::ApiGateway::Resource');
    const stream = Object.entries(resources).find(([, value]: [string, any]) => value.Properties.PathPart === 'stream');
    expect(stream).toBeDefined();
    const parent = resources[(stream![1] as any).Properties.ParentId.Ref] as any;
    expect(parent.Properties.PathPart).toBe('chat');
    template.hasResourceProperties('AWS::ApiGateway::Method', {
      ResourceId: { Ref: stream![0] },
      HttpMethod: 'POST', AuthorizationType: 'COGNITO_USER_POOLS', AuthorizationScopes: ['openid'],
      Integration: Match.objectLike({ ResponseTransferMode: 'STREAM' })
    });
  });
  it('forwards the uncached streaming path through CloudFront', () => {
    const [distribution] = Object.values(template.findResources('AWS::CloudFront::Distribution')) as any[];
    const config = distribution.Properties.DistributionConfig;
    const behavior = config.CacheBehaviors.find((value: any) => value.PathPattern === 'chat/stream');
    expect(behavior.Compress).toBe(false);
    expect(behavior.CachePolicyId).toBe('4135ea2d-6df8-44a3-9df3-4b5a84be39ad');
    expect(config.Origins.find((origin: any) => origin.Id === behavior.TargetOriginId).OriginPath).toBe('/v1');
  });
  it('creates a per-user lease table and grants the runner only the required table and router access', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      KeySchema: [{ AttributeName: 'user_id', KeyType: 'HASH' }, { AttributeName: 'record', KeyType: 'RANGE' }],
      BillingMode: 'PAY_PER_REQUEST',
    });
    template.hasResourceProperties('AWS::Lambda::Function', {
      Runtime: 'nodejs24.x', Handler: 'session-runner/index.handler', Timeout: 840,
      Environment: { Variables: Match.objectLike({ CEI_ROUTER_FUNCTION: Match.anyValue(), CEI_CHAT_HISTORY_TABLE: Match.anyValue() }) },
    });
    const functions = Object.entries(template.findResources('AWS::Lambda::Function')) as [string, any][];
    const runner = functions.find(([, fn]) => fn.Properties.Environment?.Variables?.CEI_ROUTER_FUNCTION)?.[1].Properties;
    expect(runner).toBeDefined();
    const routerId = runner.Environment.Variables.CEI_ROUTER_FUNCTION.Ref;
    expect(functions.some(([id]) => id === routerId)).toBe(true);
    const roleId = runner.Role['Fn::GetAtt'][0];
    const policies = Object.values(template.findResources('AWS::IAM::Policy')) as any[];
    const statements = policies.filter((policy) => policy.Properties.Roles.some((role: any) => role.Ref === roleId))
      .flatMap((policy) => policy.Properties.PolicyDocument.Statement)
      .filter((statement: any) => [statement.Action].flat().some((action: string) => /^(dynamodb|lambda):/.test(action)));
    const actionsFor = (resource: unknown) => statements.filter((s: any) => JSON.stringify(s.Resource).includes(JSON.stringify(resource).slice(1, -1)))
      .flatMap((s: any) => [s.Action].flat()).sort();
    const env = runner.Environment.Variables;
    expect(actionsFor(env.CEI_CHAT_METADATA_TABLE.Ref)).toEqual(['dynamodb:GetItem']);
    expect(actionsFor(env.CEI_CHAT_HISTORY_TABLE.Ref)).toEqual(['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:Query', 'dynamodb:TransactWriteItems'].sort());
    expect(actionsFor(env.CEI_ACTIVE_RUNS_TABLE.Ref)).toEqual(['dynamodb:PutItem', 'dynamodb:TransactWriteItems'].sort());
    expect(actionsFor(routerId)).toEqual(['lambda:InvokeFunction']);
    // F19/B109: the run-start account gate reads UserMetadataTable (one read-only GetItem).
    expect(actionsFor(env.CEI_USER_METADATA_TABLE.Ref)).toEqual(['dynamodb:GetItem']);
    expect(statements).toHaveLength(5);
    expect(JSON.stringify(statements)).not.toContain('"*"');
  });
  it('grants the Research role access to the internal refresh route', () => {
    const policies = Object.values(template.findResources('AWS::IAM::Policy')).map((policy) => JSON.stringify(policy));
    expect(policies.some((policy) => policy.includes('AgentRuntimesResearch') &&
      policy.includes('execute-api:Invoke') && policy.includes('internal/token-refresh'))).toBe(true);
  });
});
