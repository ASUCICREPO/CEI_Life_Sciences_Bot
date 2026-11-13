import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { BackendStack } from '../lib/backend-stack';
import { DEFAULT_LIMITS } from '../lib/config/deploy-config';

describe('operational monitoring', () => {
  const stack = new BackendStack(new cdk.App(), 'MonitoringTest', {
    env: { account: '123456789012', region: 'us-east-1' },
    frontendAssetPath: path.join(__dirname, 'fixtures', 'spa'),
    config: {
      region: 'us-east-1', defaultModelId: 'model', routerModelId: 'model', blockedModelIds: [],
      allowedEmailDomains: ['example.com'], adminEmail: 'admin@example.com', limits: DEFAULT_LIMITS,
      webSearchEnabled: true, accountDeletionEnabled: false, allowLocalhostCallback: false
    },
  });
  const template = Template.fromStack(stack);
  // Alarms and log metric filters live in the Observability nested stack (500-resource limit).
  const alarmStack = stack.node.findAll().find((c): c is cdk.NestedStack => c instanceof cdk.NestedStack && c.node.id === 'Alarms');
  const alarmsTemplate = Template.fromStack(alarmStack!);
  // Nested-stack values arrive as parameters; map each one back to the parent resource it references.
  const nestedParams: Record<string, any> = (Object.values(template.findResources('AWS::CloudFormation::Stack'))[0] as any).Properties.Parameters;
  const resolved = (value: unknown): string => JSON.stringify(value)
    .replace(/"Ref":"(referenceto[A-Za-z0-9]+)"/g, (_m, param: string) => `"Ref":"${nestedParams[param]?.Ref ?? param}"`);

  it('notifies the configured administrator and provides a dashboard', () => {
    template.hasResourceProperties('AWS::SNS::Subscription', { Protocol: 'email', Endpoint: 'admin@example.com' });
    template.resourceCountIs('AWS::CloudWatch::Dashboard', 1);
  });

  it('accepts browser diagnostics only through the Cognito-authorized API', () => {
    const resources = template.findResources('AWS::ApiGateway::Resource');
    const endpoint = Object.entries(resources).find(([, resource]: [string, any]) => resource.Properties.PathPart === 'client-diagnostics');
    expect(endpoint).toBeDefined();
    template.hasResourceProperties('AWS::ApiGateway::Method', {
      ResourceId: { Ref: endpoint![0] }, HttpMethod: 'POST', AuthorizationType: 'COGNITO_USER_POOLS',
      AuthorizationScopes: ['openid'], AuthorizerId: Match.anyValue(),
    });
    const functions = template.findResources('AWS::Lambda::Function');
    const diagnostic = Object.entries(functions).find(([id]) => id.startsWith('ClientDiagnosticsFn'))?.[1] as any;
    expect(diagnostic).toBeDefined();
    expect(Object.keys(diagnostic.Properties.Environment.Variables)).toEqual(['ALLOWED_ORIGINS']);
    expect(diagnostic.Properties.Layers).toBeUndefined();
    const filters = Object.values(alarmsTemplate.findResources('AWS::Logs::MetricFilter')) as any[];
    expect(filters.some((f) => f.Properties.FilterPattern.includes('browser_failure'))).toBe(true);
  });

  it('alarms on every application Lambda error and throttle (except consolidated functions)', () => {
    const functions = Object.entries(template.findResources('AWS::Lambda::Function')).filter(([id]) => !id.startsWith('Custom') && !id.startsWith('BucketNotificationsHandler') &&
        // CDK framework Lambda behind AwsCustomResource (attachments bucket CORS): deploy-time only, not app code
        !id.startsWith('AWS679f53fac002430cb0da5b7982bd2287'));
    const alarms = Object.values(alarmsTemplate.findResources('AWS::CloudWatch::Alarm')) as any[];
    // Some functions have throttle alarms skipped to stay within CloudFormation's 500-resource limit
    const skipThrottleAlarm = new Set(['AttachmentProcessor']);
    for (const [id] of functions) {
      // All functions have error alarms
      expect(alarms.some((a) => a.Properties.Namespace === 'AWS/Lambda' && a.Properties.MetricName === 'Errors' &&
        resolved(a.Properties.Dimensions).includes(id) && a.Properties.AlarmActions?.length)).toBe(true);
      // Only non-skipped functions have throttle alarms
      const shouldHaveThrottle = !Array.from(skipThrottleAlarm).some((skip) => id.includes(skip));
      if (shouldHaveThrottle) {
        expect(alarms.some((a) => a.Properties.Namespace === 'AWS/Lambda' && a.Properties.MetricName === 'Throttles' &&
          resolved(a.Properties.Dimensions).includes(id) && a.Properties.AlarmActions?.length)).toBe(true);
      }
    }
  });

  it('monitors handled failures, API errors, and each DynamoDB table', () => {
    alarmsTemplate.hasResourceProperties('AWS::CloudWatch::Alarm', { Metrics: Match.arrayWith([Match.objectLike({ Expression: Match.stringLikeRegexp('100 \\* errors') })]) });
    const filters = Object.values(alarmsTemplate.findResources('AWS::Logs::MetricFilter')) as any[];
    expect(filters.some((f) => f.Properties.FilterPattern.includes('stream_failed'))).toBe(true);
    expect(filters.some((f) => f.Properties.FilterPattern.includes('agent_error'))).toBe(true);
    expect(filters.some((f) => f.Properties.FilterPattern.includes('stream_transport_failed') &&
      f.Properties.FilterPattern.includes('replay_timeout'))).toBe(true);
    expect(filters.some((f) => f.Properties.FilterPattern.includes('agent_proxy_failure'))).toBe(true);
    expect(filters.some((f) => f.Properties.FilterPattern.includes('agent_proxy_invalid_json'))).toBe(true);
    expect(filters.some((f) => f.Properties.FilterPattern.includes('agent_proxy_application_error'))).toBe(true);
    expect(filters.some((f) => f.Properties.FilterPattern.includes('cms_ncd_detail_error'))).toBe(true);
    expect(filters.some((f) => f.Properties.FilterPattern.includes('api_key_lookup_failed'))).toBe(true);
    expect(filters.some((f) => f.Properties.FilterPattern.includes('ncbi_request_failed'))).toBe(true);
    expect(filters.filter((f) => f.Properties.FilterPattern.includes('handled_api_failure')).length).toBeGreaterThanOrEqual(3);
    expect(filters.some((f) => f.Properties.FilterPattern.includes('DENY'))).toBe(true);
    const tables = Object.keys(template.findResources('AWS::DynamoDB::Table'));
    const alarms = Object.values(alarmsTemplate.findResources('AWS::CloudWatch::Alarm')) as any[];
    for (const table of tables) {
      // Each table should have a throttle alarm (aggregated, no Operation dimension)
      expect(alarms.some((a) => a.Properties.AlarmActions?.length && a.Properties.Metrics?.some((m: any) =>
        m.MetricStat?.Metric?.Namespace === 'AWS/DynamoDB' && m.MetricStat.Metric.MetricName === 'ThrottledRequests' &&
        resolved(m.MetricStat.Metric.Dimensions).includes(table)))).toBe(true);
    }
  });

  it('keeps browser and stale-job signals out of the alarm set', () => {
    const alarms = Object.entries(alarmsTemplate.findResources('AWS::CloudWatch::Alarm'));
    for (const [id, alarm] of alarms as [string, any][]) {
      expect(`${id} ${alarm.Properties.AlarmDescription ?? ''}`).not.toMatch(/browser|stale.?job|agent.?health/i);
    }
  });
});
