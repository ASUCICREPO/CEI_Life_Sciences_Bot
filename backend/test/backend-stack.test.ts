import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { Annotations } from 'aws-cdk-lib/assertions';
import { BackendStack } from '../lib/backend-stack';
import { DeployConfig, DEFAULT_LIMITS } from '../lib/config/deploy-config';

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
    ...overrides,
  };
}

describe('BackendStack', () => {
  it('creates the AgentCore Code Interpreter', () => {
    const app = new cdk.App();
    const stack = new BackendStack(app, 'TestStack', { env: { region: 'us-east-1', account: '123456789012' } });
    const template = Template.fromStack(stack);
    template.resourceCountIs('AWS::BedrockAgentCore::CodeInterpreterCustom', 1);
    template.hasResourceProperties('AWS::BedrockAgentCore::CodeInterpreterCustom', {
      Name: 'ceiLifeSciencesCI',
    });
  });

  it('has no WebSearchEnabled output without a config', () => {
    const app = new cdk.App();
    const stack = new BackendStack(app, 'TestStack', { env: { region: 'us-east-1', account: '123456789012' } });
    const template = Template.fromStack(stack);
    expect(Object.keys(template.findOutputs('WebSearchEnabled'))).toHaveLength(0);
  });

  it('exposes config and a WebSearchEnabled output when a config is supplied', () => {
    const app = new cdk.App();
    const config = testConfig({ webSearchEnabled: true });
    const stack = new BackendStack(app, 'TestStack', {
      env: { region: 'us-east-1', account: '123456789012' },
      config,
    });
    expect(stack.config).toBe(config);
    const template = Template.fromStack(stack);
    template.hasOutput('WebSearchEnabled', { Value: 'true' });
  });

  it('tags the stack with Project', () => {
    const app = new cdk.App();
    const stack = new BackendStack(app, 'TestStack', { env: { region: 'us-east-1', account: '123456789012' } });
    const template = Template.fromStack(stack);
    template.hasResourceProperties(
      'AWS::BedrockAgentCore::CodeInterpreterCustom',
      Match.objectLike({
        Tags: Match.objectLike({ Project: 'CEI-Life-Sciences-Bot' }),
      }),
    );
  });

  it('produces zero unsuppressed cdk-nag AwsSolutions errors', () => {
    const app = new cdk.App();
    const stack = new BackendStack(app, 'TestStack', { env: { region: 'us-east-1', account: '123456789012' } });
    Template.fromStack(stack);
    const errors = Annotations.fromStack(stack).findError('*', Match.stringLikeRegexp('AwsSolutions-.*'));
    expect(errors).toHaveLength(0);
  });
});
