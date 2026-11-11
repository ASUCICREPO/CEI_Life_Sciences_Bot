import * as path from 'path';
import { Duration, RemovalPolicy, Stack } from 'aws-cdk-lib';
import * as apigw from 'aws-cdk-lib/aws-apigateway';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';
import { suppressXrayWildcard } from './tracing';

/** Must match STATUS_INDEX in backend/agents/shared/persistence/jobs.py. */
export const JOB_STATUS_INDEX = 'status-updated_at';

export interface BackgroundJobsProps {
  api: apigw.RestApi;
  methodOptions: apigw.MethodOptions;
  corsOptions: apigw.CorsOptions;
  requestValidator: apigw.IRequestValidator;
  pythonDepsLayer: lambda.ILayerVersion;
  activeRunsTable: dynamodb.ITable;
  allowedOrigins: string[];
  /** agent_type (research | drafting | analysis | pipeline_manager) -> AgentCore StopRuntimeSession URL. */
  runtimeStopUrls: Record<string, string>;
}

/**
 * F16: Job ID Store, Polling Lambda (GET /jobs, GET /jobs/{jobId}, POST /jobs/{jobId}/cancel) and the
 * scheduled watchdog. Runtimes create and update jobs; clients only read and cancel them.
 */
export class BackgroundJobs extends Construct {
  public readonly table: dynamodb.Table;
  public readonly jobsFunction: lambda.Function;
  public readonly watchdogFunction: lambda.Function;

  constructor(scope: Construct, id: string, props: BackgroundJobsProps) {
    super(scope, id);

    this.table = new dynamodb.Table(this, 'JobIdStore', {
      partitionKey: { name: 'user_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'job_id', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      // ADR: RETAIN | Rationale: job records reference user chats and are removed by the deletion cascade (F19), not by a stack teardown
      removalPolicy: RemovalPolicy.RETAIN,
    });
    // ADR: KEYS_ONLY status index | Rationale: the watchdog's conditional writes need only user_id, job_id, status and
    // updated_at, which are all keys of the table or index | Alternative: ALL rejected (copies step lists and surfaces)
    this.table.addGlobalSecondaryIndex({
      indexName: JOB_STATUS_INDEX,
      partitionKey: { name: 'status', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'updated_at', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.KEYS_ONLY,
    });
    const indexArn = `${this.table.tableArn}/index/${JOB_STATUS_INDEX}`;

    const env = {
      CEI_JOB_TABLE: this.table.tableName,
      CEI_ACTIVE_RUNS_TABLE: props.activeRunsTable.tableName,
    };
    const jobs = this.pythonFunction('JobsHandler', 'jobs-handler', props.pythonDepsLayer, Duration.seconds(15), {
      ...env,
      ALLOWED_ORIGINS: props.allowedOrigins.join(','),
      // ADR: Stack.toJsonString | Rationale: the URLs hold deploy-time tokens; same as AGENT_URLS on the agent proxy
      CEI_RUNTIME_STOP_URLS: Stack.of(this).toJsonString(props.runtimeStopUrls),
    });
    jobs.role.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['dynamodb:GetItem', 'dynamodb:Query', 'dynamodb:UpdateItem'], resources: [this.table.tableArn],
    }));
    jobs.role.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['dynamodb:UpdateItem'], resources: [props.activeRunsTable.tableArn],
    }));
    this.jobsFunction = jobs.fn;

    const watchdog = this.pythonFunction('JobWatchdog', 'watchdog', props.pythonDepsLayer, Duration.minutes(1), {
      ...env,
      CEI_JOB_STALE_SECONDS: '600',
      CEI_JOB_PENDING_SECONDS: '3600',
      // Off until a runtime can resume a requeued job from its checkpoint (F16 task 2).
      CEI_WATCHDOG_RESUME_ENABLED: 'false',
      CEI_JOB_MAX_RECOVERIES: '1',
    });
    watchdog.role.addToPrincipalPolicy(new iam.PolicyStatement({ actions: ['dynamodb:Query'], resources: [indexArn] }));
    watchdog.role.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['dynamodb:UpdateItem'], resources: [this.table.tableArn, props.activeRunsTable.tableArn],
    }));
    this.watchdogFunction = watchdog.fn;
    new events.Rule(this, 'WatchdogSchedule', {
      schedule: events.Schedule.rate(Duration.minutes(5)),
      targets: [new targets.LambdaFunction(watchdog.fn, { retryAttempts: 0 })],
    });

    const integration = new apigw.LambdaIntegration(jobs.fn);
    const jobsResource = props.api.root.addResource('jobs', { defaultCorsPreflightOptions: props.corsOptions });
    jobsResource.addMethod('GET', integration, props.methodOptions);
    const jobResource = jobsResource.addResource('{jobId}', { defaultCorsPreflightOptions: props.corsOptions });
    jobResource.addMethod('GET', integration, props.methodOptions);
    jobResource.addResource('cancel', { defaultCorsPreflightOptions: props.corsOptions })
      .addMethod('POST', integration, props.methodOptions);
    for (const child of jobsResource.node.findAll()) {
      if (child instanceof apigw.CfnMethod && child.httpMethod === 'OPTIONS') {
        child.requestValidatorId = props.requestValidator.requestValidatorId;
        NagSuppressions.addResourceSuppressions(child, [
          { id: 'AwsSolutions-APIG4', reason: 'ADR: CORS preflight without authorizer | Rationale: browsers do not send credentials on preflight | Alternative: authenticated preflight rejected because browsers cannot satisfy it' },
          { id: 'AwsSolutions-COG4', reason: 'ADR: CORS preflight without Cognito | Rationale: browsers do not send credentials on preflight | Alternative: authenticated preflight rejected because browsers cannot satisfy it' },
        ]);
      }
    }
  }

  private pythonFunction(id: string, dir: string, layer: lambda.ILayerVersion, timeout: Duration,
    environment: Record<string, string>): { fn: lambda.Function; role: iam.Role } {
    const logGroup = new logs.LogGroup(this, `${id}Logs`, {
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: RemovalPolicy.DESTROY,
    });
    // ADR: hand-rolled role, no AWSLambdaBasicExecutionRole | Rationale: same as the session Lambdas — avoids IAM4
    const role = new iam.Role(this, `${id}Role`, { assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com') });
    logGroup.grantWrite(role);
    const fn = new lambda.Function(this, `${id}Fn`, {
      tracing: lambda.Tracing.ACTIVE,
      runtime: lambda.Runtime.PYTHON_3_14,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'lambdas', dir), {
        exclude: ['tests', '__pycache__', '*.pyc', '.pytest_cache'],
      }),
      layers: [layer],
      role,
      logGroup,
      timeout,
      memorySize: 256,
      environment,
    });
    suppressXrayWildcard(role);
    return { fn, role };
  }
}
