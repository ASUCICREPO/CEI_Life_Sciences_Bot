import * as cdk from 'aws-cdk-lib';
import { Aspects } from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { AwsSolutionsChecks } from 'cdk-nag';
import { aws_wafv2 as wafv2 } from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import { ALERT_TOPIC_NAME } from './constructs/observability';

export interface EdgeWafStackProps extends cdk.StackProps {
  /** Region hosting the shared operational SNS topic. */
  alertTopicRegion?: string;
}

/**
 * Small, always-us-east-1 stack holding only the CLOUDFRONT-scope Web ACL.
 * AWS only accepts a CLOUDFRONT-scope web ACL in us-east-1, so this stays a
 * separate stack from `BackendStack` (B88). `BackendStack` consumes
 * `webAclArn` through `crossRegionReferences: true`.
 */
export class EdgeWafStack extends cdk.Stack {
  public readonly webAclArn: string;

  constructor(scope: Construct, id: string, props?: EdgeWafStackProps) {
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

    if (props?.alertTopicRegion) {
      // CloudFront WAF metrics live in us-east-1 even if the application is in another region.
      // Initial threshold: 25% blocked with at least 100 requests in five minutes.
      const blocked = new cloudwatch.Metric({ namespace: 'AWS/WAFV2', metricName: 'BlockedRequests',
        dimensionsMap: { WebACL: 'cei-life-sciences-edge-waf', Rule: 'ALL' }, statistic: 'Sum', period: cdk.Duration.minutes(5) });
      const allowed = new cloudwatch.Metric({ namespace: 'AWS/WAFV2', metricName: 'AllowedRequests',
        dimensionsMap: { WebACL: 'cei-life-sciences-edge-waf', Rule: 'ALL' }, statistic: 'Sum', period: cdk.Duration.minutes(5) });
      const rate = new cloudwatch.MathExpression({ expression: 'IF(blocked + allowed >= 100, 100 * blocked / (blocked + allowed), 0)',
        usingMetrics: { blocked, allowed }, period: cdk.Duration.minutes(5) });
      const alarm = new cloudwatch.Alarm(this, 'BlockedRequestsSpike', {
        metric: rate, threshold: 25, evaluationPeriods: 1,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
        alarmDescription: 'WAF blocked at least 25% of 100 or more requests in five minutes. Starting threshold; tune after observing traffic.',
      });
      // Use the stable topic ARN to avoid an Edge -> Backend -> Edge stack dependency cycle.
      (alarm.node.defaultChild as cloudwatch.CfnAlarm).alarmActions = [
        `arn:${cdk.Aws.PARTITION}:sns:${props.alertTopicRegion}:${cdk.Aws.ACCOUNT_ID}:${ALERT_TOPIC_NAME}`,
      ];
    }

    new cdk.CfnOutput(this, 'WebAclArn', {
      value: this.webAclArn,
      description: 'CLOUDFRONT-scope WAF Web ACL ARN, consumed by BackendStack via crossRegionReferences',
    });
  }
}
