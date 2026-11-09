import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Annotations, Match, Template } from 'aws-cdk-lib/assertions';
import { buildStacks } from '../lib/app';
import { DeployConfig, DEFAULT_LIMITS } from '../lib/config/deploy-config';

const FIXTURE_ASSET_PATH = path.join(__dirname, 'fixtures', 'spa');

function testConfig(overrides: Partial<DeployConfig> = {}): DeployConfig {
  return {
    region: 'eu-west-1',
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

describe('buildStacks (the same wiring bin/backend.ts uses)', () => {
  const ORIGINAL_ACCOUNT = process.env.CDK_DEFAULT_ACCOUNT;

  beforeEach(() => {
    process.env.CDK_DEFAULT_ACCOUNT = '123456789012';
  });

  afterEach(() => {
    if (ORIGINAL_ACCOUNT === undefined) {
      delete process.env.CDK_DEFAULT_ACCOUNT;
    } else {
      process.env.CDK_DEFAULT_ACCOUNT = ORIGINAL_ACCOUNT;
    }
  });

  it('creates EdgeWafStack in us-east-1 and BackendStack in the deploy Region (not us-east-1)', () => {
    const app = new cdk.App();
    const { edgeWaf, backend } = buildStacks(app, testConfig({ region: 'eu-west-1' }), FIXTURE_ASSET_PATH);

    expect(edgeWaf).toBeDefined();
    expect(edgeWaf!.stackName).toBe('EdgeWafStack');
    expect(edgeWaf!.region).toBe('us-east-1');
    expect(backend.stackName).toBe('BackendStack');
    expect(backend.region).toBe('eu-west-1');
  });

  it('attaches the EdgeWafStack web ACL to the BackendStack CloudFront distribution', () => {
    const app = new cdk.App();
    const { backend } = buildStacks(app, testConfig({ region: 'eu-west-1' }), FIXTURE_ASSET_PATH);
    const template = Template.fromStack(backend);
    template.hasResourceProperties('AWS::CloudFront::Distribution', {
      DistributionConfig: Match.objectLike({ WebACLId: Match.anyValue() }),
    });
  });

  it('sends the us-east-1 WAF alarm to the shared topic in the application region', () => {
    const app = new cdk.App();
    const { edgeWaf, backend } = buildStacks(app, testConfig({ region: 'eu-west-1' }), FIXTURE_ASSET_PATH);
    Template.fromStack(backend).hasResourceProperties('AWS::SNS::Topic', { TopicName: 'cei-life-sciences-alerts' });
    const alarms = Object.values(Template.fromStack(edgeWaf!).findResources('AWS::CloudWatch::Alarm')) as any[];
    expect(alarms).toHaveLength(1);
    const serialized = JSON.stringify(alarms[0].Properties);
    expect(serialized).toContain('AWS/WAFV2');
    expect(serialized).toContain('BlockedRequests');
    expect(serialized).toContain('eu-west-1');
    expect(serialized).toContain('cei-life-sciences-alerts');
  });

  it('produces zero unsuppressed cdk-nag AwsSolutions errors on both cross-region stacks', () => {
    const app = new cdk.App();
    const { edgeWaf, backend } = buildStacks(app, testConfig({ region: 'eu-west-1' }), FIXTURE_ASSET_PATH);

    // Synthesizing the whole app (not just Template.fromStack per stack) is what actually resolves the
    // cross-region SSM export/import and adds the CrossRegionExportReader/Writer custom resources.
    app.synth();

    const backendErrors = Annotations.fromStack(backend).findError('*', Match.stringLikeRegexp('AwsSolutions-.*'));
    const edgeWafErrors = Annotations.fromStack(edgeWaf!).findError('*', Match.stringLikeRegexp('AwsSolutions-.*'));

    expect(backendErrors).toHaveLength(0);
    expect(edgeWafErrors).toHaveLength(0);
  });
});
