import * as cdk from 'aws-cdk-lib';
import { Aspects } from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { AwsSolutionsChecks } from 'cdk-nag';
import { aws_wafv2 as wafv2 } from 'aws-cdk-lib';

/**
 * Small, always-us-east-1 stack holding only the CLOUDFRONT-scope Web ACL.
 * AWS only accepts a CLOUDFRONT-scope web ACL in us-east-1, so this stays a
 * separate stack from `BackendStack` (B88). `BackendStack` consumes
 * `webAclArn` through `crossRegionReferences: true`.
 */
export class EdgeWafStack extends cdk.Stack {
  public readonly webAclArn: string;

  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // ADR: cdk-nag scoped to stack, not app entry point | Rationale: matches BackendStack's convention (self-contained checks travel with the stack)
    Aspects.of(this).add(new AwsSolutionsChecks({ verbose: true }));

    cdk.Tags.of(this).add('Project', 'CEI-Life-Sciences-Bot');

    // ADR: rate-based rule 1000 req / 5 min per IP | Rationale: B88 default proposal (security.md T5 mitigation, values TBD)
    const webAcl = new wafv2.CfnWebACL(this, 'EdgeWebAcl', {
      name: 'cei-life-sciences-edge-waf',
      description: 'CloudFront-scope WAF for the CEI Life Sciences Bot dashboard',
      scope: 'CLOUDFRONT',
      defaultAction: { allow: {} },
      visibilityConfig: {
        sampledRequestsEnabled: true,
        cloudWatchMetricsEnabled: true,
        metricName: 'cei-life-sciences-edge-waf',
      },
      rules: [
        {
          name: 'AWS-AWSManagedRulesCommonRuleSet',
          priority: 0,
          overrideAction: { none: {} },
          statement: {
            managedRuleGroupStatement: {
              vendorName: 'AWS',
              name: 'AWSManagedRulesCommonRuleSet',
            },
          },
          visibilityConfig: {
            sampledRequestsEnabled: true,
            cloudWatchMetricsEnabled: true,
            metricName: 'AWSManagedRulesCommonRuleSet',
          },
        },
        {
          name: 'AWS-AWSManagedRulesKnownBadInputsRuleSet',
          priority: 1,
          overrideAction: { none: {} },
          statement: {
            managedRuleGroupStatement: {
              vendorName: 'AWS',
              name: 'AWSManagedRulesKnownBadInputsRuleSet',
            },
          },
          visibilityConfig: {
            sampledRequestsEnabled: true,
            cloudWatchMetricsEnabled: true,
            metricName: 'AWSManagedRulesKnownBadInputsRuleSet',
          },
        },
        {
          name: 'AWS-AWSManagedRulesAmazonIpReputationList',
          priority: 2,
          overrideAction: { none: {} },
          statement: {
            managedRuleGroupStatement: {
              vendorName: 'AWS',
              name: 'AWSManagedRulesAmazonIpReputationList',
            },
          },
          visibilityConfig: {
            sampledRequestsEnabled: true,
            cloudWatchMetricsEnabled: true,
            metricName: 'AWSManagedRulesAmazonIpReputationList',
          },
        },
        {
          name: 'RateLimitPerIp',
          priority: 3,
          action: { block: {} },
          statement: {
            rateBasedStatement: {
              limit: 1000,
              evaluationWindowSec: 300,
              aggregateKeyType: 'IP',
            },
          },
          visibilityConfig: {
            sampledRequestsEnabled: true,
            cloudWatchMetricsEnabled: true,
            metricName: 'RateLimitPerIp',
          },
        },
      ],
    });

    this.webAclArn = webAcl.attrArn;

    new cdk.CfnOutput(this, 'WebAclArn', {
      value: this.webAclArn,
      description: 'CLOUDFRONT-scope WAF Web ACL ARN, consumed by BackendStack via crossRegionReferences',
    });
  }
}
