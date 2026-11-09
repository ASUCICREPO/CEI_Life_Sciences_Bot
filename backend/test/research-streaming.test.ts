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

  it('uses a Cognito-authorized REST streaming integration', () => {
    template.hasResourceProperties('AWS::ApiGateway::Method', {
      HttpMethod: 'POST', AuthorizationType: 'COGNITO_USER_POOLS',
      Integration: Match.objectLike({ ResponseTransferMode: 'STREAM' }),
    });
  });
  it('forwards the uncached streaming path through CloudFront', () => {
    template.hasResourceProperties('AWS::CloudFront::Distribution', {
      DistributionConfig: Match.objectLike({
        CacheBehaviors: Match.arrayWith([Match.objectLike({ PathPattern: 'chat/stream', Compress: false, CachePolicyId: '4135ea2d-6df8-44a3-9df3-4b5a84be39ad' })]),
        Origins: Match.arrayWith([Match.objectLike({ OriginPath: '/v1' })]),
      }),
    });
  });
  it('creates a per-user lease table and grants the runner only the required table and router access', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      KeySchema: [{ AttributeName: 'user_id', KeyType: 'HASH' }, { AttributeName: 'record', KeyType: 'RANGE' }],
      BillingMode: 'PAY_PER_REQUEST',
    });
    template.hasResourceProperties('AWS::Lambda::Function', {
      Runtime: 'nodejs22.x', Handler: 'index.handler', Timeout: 840,
      Environment: { Variables: Match.objectLike({ CEI_ROUTER_FUNCTION: Match.anyValue(), CEI_CHAT_HISTORY_TABLE: Match.anyValue() }) },
    });
  });
});
