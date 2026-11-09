import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { StreamingSpikeStack } from '../lib/spikes/streaming-spike-stack';

describe('S1 streaming spike', () => {
  it('uses an authenticated streaming integration and disables edge caching and compression', () => {
    const app = new cdk.App();
    const stack = new StreamingSpikeStack(app, 'StreamingSpike', { env: { account: '123456789012', region: 'us-east-2' } });
    const template = Template.fromStack(stack);
    template.hasResourceProperties('AWS::ApiGateway::Method', {
      HttpMethod: 'POST',
      AuthorizationType: 'COGNITO_USER_POOLS',
      Integration: Match.objectLike({ ResponseTransferMode: 'STREAM' }),
    });
    template.hasResourceProperties('AWS::CloudFront::Distribution', {
      DistributionConfig: Match.objectLike({
        DefaultCacheBehavior: Match.objectLike({ Compress: false, CachePolicyId: '4135ea2d-6df8-44a3-9df3-4b5a84be39ad' }),
      }),
    });
  });
});
