import * as cdk from 'aws-cdk-lib';
import { Template, Match, Annotations } from 'aws-cdk-lib/assertions';
import { EdgeWafStack } from '../lib/edge-waf-stack';
import { memoSynth } from './helpers/memo-synth';

// ADR: all tests here build the same stack and only read it | one shared build per unique config
function built(): { stack: EdgeWafStack; template: Template } {
  return memoSynth('EdgeWafStack:default', () => {
    const app = new cdk.App();
    const stack = new EdgeWafStack(app, 'TestEdgeWafStack', {
      env: { region: 'us-east-1', account: '123456789012' },
    });
    return { stack, template: Template.fromStack(stack) };
  });
}

describe('EdgeWafStack', () => {
  it('creates a CLOUDFRONT-scope web ACL with the rate rule', () => {
    const { template } = built();

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
    const { template } = built();
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
    const { template } = built();
    expect(Object.keys(template.findOutputs('WebAclArn'))).toHaveLength(1);
  });

  it('tags the stack with Project', () => {
    const { template } = built();
    template.hasResourceProperties('AWS::WAFv2::WebACL', Match.objectLike({ Tags: Match.arrayWith([]) }));
  });

  it('produces zero unsuppressed cdk-nag AwsSolutions errors', () => {
    const { stack } = built();
    const errors = Annotations.fromStack(stack).findError('*', Match.stringLikeRegexp('AwsSolutions-.*'));
    expect(errors).toHaveLength(0);
  });
});
