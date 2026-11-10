import * as cdk from 'aws-cdk-lib';
import { Annotations, Match, Template } from 'aws-cdk-lib/assertions';
import { IdentitySpikeStack } from '../lib/spikes/identity-spike-stack';

describe('B99 isolated identity spike', () => {
  const stack = new IdentitySpikeStack(new cdk.App(), 'IdentitySpike', {
    env: { account: '123456789012', region: 'us-east-1' },
  });
  const template = Template.fromStack(stack);

  test('uses verified JWTs, a request interceptor and an enforced policy', () => {
    template.hasResourceProperties('AWS::BedrockAgentCore::Gateway', {
      AuthorizerType: 'CUSTOM_JWT',
      InterceptorConfigurations: [{ InterceptionPoints: ['REQUEST'],
        InputConfiguration: { PassRequestHeaders: true }, Interceptor: { Lambda: { Arn: Match.anyValue() } } }],
      PolicyEngineConfiguration: Match.objectLike({ Mode: 'ENFORCE' }),
    });
    template.hasResourceProperties('AWS::BedrockAgentCore::GatewayTarget', {
      Name: 'identity-probe', CredentialProviderConfigurations: [{ CredentialProviderType: 'GATEWAY_IAM_ROLE' }],
      TargetConfiguration: { Mcp: { Lambda: { LambdaArn: Match.anyValue(), ToolSchema: { InlinePayload: Match.anyValue() } } } },
    });
    expect(JSON.stringify(template.findResources('AWS::BedrockAgentCore::Policy'))).toContain('cei-spike/probe');
    template.hasResourceProperties('AWS::IAM::Policy', { PolicyDocument: { Statement: Match.arrayWith([
      Match.objectLike({ Effect: 'Deny', Action: 'lambda:InvokeFunction', Resource: Match.anyValue() }),
    ]) } });
  });

  test('passes security checks', () => {
    expect(Annotations.fromStack(stack).findError('*', Match.stringLikeRegexp('AwsSolutions-.*'))).toHaveLength(0);
  });
});
