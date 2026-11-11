import * as cdk from 'aws-cdk-lib';
import { Match, Template, Annotations } from 'aws-cdk-lib/assertions';
import { DurableSpikeStack } from '../lib/spikes/durable-spike-stack';

describe('isolated durable workflow spike', () => {
  const stack = new DurableSpikeStack(new cdk.App(), 'DurableSpike', {
    env: { account: '123456789012', region: 'us-east-1' },
  });
  const template = Template.fromStack(stack);

  test('uses isolated retained-duration memory and a synthetic cancellation table', () => {
    template.hasResourceProperties('AWS::BedrockAgentCore::Memory', { EventExpiryDuration: 365 });
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      KeySchema: [{ AttributeName: 'job_id', KeyType: 'HASH' }], BillingMode: 'PAY_PER_REQUEST',
    });
  });

  test('creates separate IAM-authenticated runtimes with isolated memory', () => {
    const runtimes = Object.values(template.findResources('AWS::BedrockAgentCore::Runtime')) as any[];
    expect(runtimes).toHaveLength(2);
    for (const runtime of runtimes) {
      expect(runtime.Properties.AuthorizerConfiguration).toBeUndefined(); // IAM is the default
      expect(runtime.Properties.EnvironmentVariables.CEI_SPIKE_MEMORY_ID).toBeDefined();
    }
    const asyncRuntime = runtimes.find((r) => r.Properties.AgentRuntimeName === 'CEIDurableSpikeAsync');
    const hitlRuntime = runtimes.find((r) => r.Properties.AgentRuntimeName === 'CEIDurableSpikeHitl');
    expect(asyncRuntime.Properties.EnvironmentVariables.CEI_SPIKE_CANCEL_TABLE).toBeDefined();
    expect(hitlRuntime.Properties.EnvironmentVariables.CEI_SPIKE_CANCEL_TABLE).toBeUndefined();
  });

  test('passes security checks', () => {
    expect(Annotations.fromStack(stack).findError('*', Match.stringLikeRegexp('AwsSolutions-.*'))).toHaveLength(0);
  });
});
