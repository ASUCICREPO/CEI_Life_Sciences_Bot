import { Duration } from 'aws-cdk-lib';
import * as apigw from 'aws-cdk-lib/aws-apigateway';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as actions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as subscriptions from 'aws-cdk-lib/aws-sns-subscriptions';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';

export const ALERT_TOPIC_NAME = 'cei-life-sciences-alerts';

export interface ObservabilityProps {
  api: apigw.RestApi;
  functions: Record<string, lambda.Function>;
  tables: Record<string, dynamodb.Table>;
  adminEmail?: string;
  caughtFailures: Record<string, { logGroup: logs.ILogGroup; terms: string[] }>;
  signupLogs: logs.ILogGroup;
  gatewayLogs: logs.ILogGroup;
  clientLogs: logs.ILogGroup;
  agentProxyLogs: logs.ILogGroup;
}

/** Operational signals for the resources that are deployed today. */
export class Observability extends Construct {
  constructor(scope: Construct, id: string, props: ObservabilityProps) {
    super(scope, id);
    const topic = new sns.Topic(this, 'Alerts', { enforceSSL: true, topicName: ALERT_TOPIC_NAME });
    // CloudWatch requires a customer-managed key for encrypted SNS, while B52 disallows one.
    // Alarm descriptions contain only resource names and operational thresholds, never user data.
    NagSuppressions.addResourceSuppressions(topic, [{ id: 'AwsSolutions-SNS2',
      reason: 'ADR: Alarm topic without KMS encryption | Rationale: CloudWatch requires a customer-managed key for encrypted SNS and B52 excludes customer-managed keys; alarm messages contain operational metadata only | Alternative: AWS-managed SNS key rejected because CloudWatch cannot be granted access to it',
    }]);
    if (props.adminEmail) topic.addSubscription(new subscriptions.EmailSubscription(props.adminEmail));
    const notify = new actions.SnsAction(topic);
    const dashboard = new cloudwatch.Dashboard(this, 'Dashboard', { dashboardName: `${scope.node.id}-operations` });
    const interval = Duration.minutes(5);
    const alarm = (id: string, metric: cloudwatch.IMetric, threshold: number, description: string) => {
      const resource = new cloudwatch.Alarm(this, id, {
        metric, threshold, evaluationPeriods: 1,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
        alarmDescription: `${description} Starting threshold; tune after observing production traffic.`,
      });
      resource.addAlarmAction(notify);
    };

    // Initial threshold: 5% server errors over five minutes. No traffic evaluates to zero.
    const apiRate = new cloudwatch.MathExpression({
      expression: 'IF(requests > 0, 100 * errors / requests, 0)',
      usingMetrics: {
        errors: props.api.metricServerError({ period: interval, statistic: 'Sum' }),
        requests: props.api.metricCount({ period: interval, statistic: 'Sum' }),
      },
      period: interval, label: 'API 5xx %',
    });
    alarm('Api5xxRate', apiRate, 5, 'API Gateway 5xx rate reached 5%.');
    dashboard.addWidgets(new cloudwatch.GraphWidget({ title: 'API server errors', left: [apiRate] }));

    for (const [name, fn] of Object.entries(props.functions)) {
      // One error or throttle in five minutes is the initial, deliberately sensitive threshold.
      alarm(`${name}Errors`, fn.metricErrors({ period: interval, statistic: 'Sum' }), 1, `${name} Lambda failed.`);
      alarm(`${name}Throttles`, fn.metricThrottles({ period: interval, statistic: 'Sum' }), 1, `${name} Lambda throttled.`);
    }
    dashboard.addWidgets(new cloudwatch.GraphWidget({ title: 'Lambda errors',
      left: Object.entries(props.functions).map(([name, fn]) => fn.metricErrors({ period: interval, statistic: 'Sum', label: name })) }));
    for (const [name, table] of Object.entries(props.tables)) {
      const tableMetrics: cloudwatch.IMetric[] = [];
      // One throttled request in five minutes indicates the workload was affected.
      // ThrottledRequests needs TableName and Operation. Split the operation metrics so each
      // alarm stays under CloudWatch's ten-metric expression limit.
      const operations = Object.values(dynamodb.Operation);
      for (let i = 0; i < operations.length; i += 7) {
        const metric = table.metricThrottledRequestsForOperations({ period: interval, statistic: 'Sum',
          operations: operations.slice(i, i + 7), label: `${name} operations ${i + 1}-${Math.min(i + 7, operations.length)}` });
        tableMetrics.push(metric);
        alarm(`${name}Throttles${i}`, metric, 1, `${name} DynamoDB requests throttled.`);
      }
      dashboard.addWidgets(new cloudwatch.GraphWidget({ title: `${name} throttling`, left: tableMetrics }));
    }

    // These handlers return errors normally, so Lambda's Errors metric cannot see them.
    const handledFailureMetrics: cloudwatch.IMetric[] = [];
    for (const [name, entry] of Object.entries(props.caughtFailures)) {
      const filter = new logs.MetricFilter(this, `${name}FailureFilter`, {
        logGroup: entry.logGroup,
        metricNamespace: 'CEI/Application', metricName: `${scope.node.id}-${name}-Failures`,
        filterPattern: logs.FilterPattern.anyTerm(...entry.terms), metricValue: '1',
      });
      const metric = filter.metric({ period: interval, statistic: 'Sum', label: name });
      handledFailureMetrics.push(metric);
      alarm(`${name}CaughtFailure`, metric, 1, `${name} reported a handled failure.`);
    }
    dashboard.addWidgets(new cloudwatch.GraphWidget({ title: 'Handled failures', left: handledFailureMetrics }));
    // AgentCore can return an application error inside a successful HTTP response.
    // Keep this visible without adding an agent-health alarm (B75).
    const agentApplicationErrors = new logs.MetricFilter(this, 'AgentApplicationErrorsFilter', {
      logGroup: props.agentProxyLogs, filterPattern: logs.FilterPattern.anyTerm('agent_proxy_application_error'),
      metricNamespace: 'CEI/Application', metricName: `${scope.node.id}-AgentApplicationErrors`, metricValue: '1',
    }).metric({ period: interval, statistic: 'Sum', label: 'Agent application errors' });
    dashboard.addWidgets(new cloudwatch.GraphWidget({ title: 'Agent application errors', left: [agentApplicationErrors] }));
    // Dashboard only: B75 does not include a separate browser-failure alarm.
    const browser = new logs.MetricFilter(this, 'BrowserFailuresFilter', {
      logGroup: props.clientLogs, filterPattern: logs.FilterPattern.anyTerm('browser_failure'),
      metricNamespace: 'CEI/Application', metricName: `${scope.node.id}-BrowserFailures`, metricValue: '1',
    }).metric({ period: interval, statistic: 'Sum', label: 'Browser failures' });
    dashboard.addWidgets(new cloudwatch.GraphWidget({ title: 'Browser failures', left: [browser] }));
    // Starting security thresholds: 20 sign-up attempts or 5 policy denies in five minutes.
    const securityMetrics: cloudwatch.IMetric[] = [];
    // TODO(F8): add a Guardrail intervention alarm after the production Guardrail
    // and its verified metric are connected. The existing Guardrail construct is
    // not wired to the application and has no traffic to monitor.
    // TODO(F20): confirm the Gateway log schema in the deployed environment;
    // the DENY filter below is provisional until a real policy denial is observed.
    for (const [name, logGroup, term, threshold] of [
      ['SignUpAttempts', props.signupLogs, 'pre_signup_domain_check', 20],
      ['PolicyDenials', props.gatewayLogs, 'DENY', 5],
    ] as const) {
      const filter = new logs.MetricFilter(this, `${name}Filter`, {
        logGroup, filterPattern: logs.FilterPattern.anyTerm(term),
        metricNamespace: 'CEI/Security', metricName: `${scope.node.id}-${name}`, metricValue: '1',
      });
      const metric = filter.metric({ period: interval, statistic: 'Sum', label: name });
      securityMetrics.push(metric);
      alarm(`${name}Spike`, metric, threshold, `${name} exceeded the starting threshold.`);
    }
    dashboard.addWidgets(new cloudwatch.GraphWidget({ title: 'Security events', left: securityMetrics }));
  }
}
