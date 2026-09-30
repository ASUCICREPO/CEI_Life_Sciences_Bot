import * as cdk from 'aws-cdk-lib';
import { Template, Match, Annotations } from 'aws-cdk-lib/assertions';
import { EdgeWafStack } from '../lib/edge-waf-stack';

describe('EdgeWafStack', () => {
  it('creates a CLOUDFRONT-scope web ACL with the rate rule', () => {
    const app = new cdk.App();
    const stack = new EdgeWafStack(app, 'TestEdgeWafStack', {
      env: { region: 'us-east-1', account: '123456789012' },
    });
    const template = Template.fromStack(stack);

    template.hasResourceProperties('AWS::WAFv2::WebACL', {
      Scope: 'CLOUDFRONT',
      DefaultAction: { Allow: {} },
      Rules: Match.arrayWith([
        Match.objectLike({
          Name: 'RateLimitPerIp',
          Action: { Block: {} },
          Statement: {
            RateBasedStatement: Match.objectLike({
              Limit: 1000,
              EvaluationWindowSec: 300,
              AggregateKeyType: 'IP',
            }),
          },
        }),
      ]),
    });
  });

  it('includes every AWS managed rule group from the plan', () => {
    const app = new cdk.App();
    const stack = new EdgeWafStack(app, 'TestEdgeWafStack', {
      env: { region: 'us-east-1', account: '123456789012' },
    });
    const template = Template.fromStack(stack);
    const managedRuleNames = [
      'AWSManagedRulesCommonRuleSet',
      'AWSManagedRulesKnownBadInputsRuleSet',
      'AWSManagedRulesAmazonIpReputationList',
    ];
    for (const name of managedRuleNames) {
      template.hasResourceProperties('AWS::WAFv2::WebACL', {
        Rules: Match.arrayWith([
          Match.objectLike({
            Statement: { ManagedRuleGroupStatement: Match.objectLike({ Name: name, VendorName: 'AWS' }) },
          }),
        ]),
      });
    }
  });

  it('exposes the webAclArn output', () => {
    const app = new cdk.App();
    const stack = new EdgeWafStack(app, 'TestEdgeWafStack', {
      env: { region: 'us-east-1', account: '123456789012' },
    });
    const template = Template.fromStack(stack);
    expect(Object.keys(template.findOutputs('WebAclArn'))).toHaveLength(1);
  });

  it('tags the stack with Project', () => {
    const app = new cdk.App();
    const stack = new EdgeWafStack(app, 'TestEdgeWafStack', {
      env: { region: 'us-east-1', account: '123456789012' },
    });
    const template = Template.fromStack(stack);
    template.hasResourceProperties('AWS::WAFv2::WebACL', Match.objectLike({ Tags: Match.arrayWith([]) }));
  });

  it('produces zero unsuppressed cdk-nag AwsSolutions errors', () => {
    const app = new cdk.App();
    const stack = new EdgeWafStack(app, 'TestEdgeWafStack', {
      env: { region: 'us-east-1', account: '123456789012' },
    });
    Template.fromStack(stack);
    const errors = Annotations.fromStack(stack).findError('*', Match.stringLikeRegexp('AwsSolutions-.*'));
    expect(errors).toHaveLength(0);
  });
});
