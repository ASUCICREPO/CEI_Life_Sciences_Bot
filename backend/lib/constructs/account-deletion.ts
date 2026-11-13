import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Duration, RemovalPolicy } from 'aws-cdk-lib';
import * as apigw from 'aws-cdk-lib/aws-apigateway';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as scheduler from 'aws-cdk-lib/aws-scheduler';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';
import { suppressXrayWildcard } from './tracing';

export interface AccountDeletionProps {
  api: apigw.RestApi;
  methodOptions: apigw.MethodOptions;
  corsOptions: apigw.CorsOptions;
  requestValidator: apigw.IRequestValidator;
  pythonDepsLayer: lambda.ILayerVersion;
  allowedOrigins: string[];
  userPool: cognito.UserPool;
  userMetadataTable: dynamodb.Table;
  chatMetadataTable: dynamodb.Table;
  chatHistoryTable: dynamodb.Table;
  activeRunsTable: dynamodb.Table;
  exportsTable: dynamodb.Table;
  exportsBucket: s3.Bucket;
  jobTable: dynamodb.Table;
  /** B78 prefix (`cei/user-keys`); the worker loops `apiKeySourceIds` under it, never ListSecrets. */
  apiKeySecretPrefix: string;
  apiKeySourceIds: string[];
  /** The one shared AgentCore Memory resource (agent-runtimes.ts `researchMemory`). */
  memoryId: string;
  memoryArn: string;
}

/**
 * F19/B109: `POST /account/delete` and the scheduled delete-worker it hands off to. Exists in the stack
 * only when `accountDeletionEnabled` is true (backend-stack.ts) -- the run-start gate *check* itself
 * (reading the UserMetadataTable "deletion" row) stays wired unconditionally in every gated Lambda, so
 * that read-only code path is always live even when this construct, and the route, are not created.
 *
 * The one-time EventBridge Scheduler schedule per user is created at runtime (`scheduler:CreateSchedule`,
 * `ActionAfterCompletion: DELETE`, `FlexibleTimeWindow: OFF`) by the account-delete Lambda, and re-created
 * by the delete-worker itself when it reschedules (early/near-timeout); there is no CFN `CfnSchedule`
 * resource here, only the dedicated `ScheduleGroup` and the one execution role both Lambdas may pass.
 */
export class AccountDeletion extends Construct {
  public readonly deleteFunction: lambda.Function;
  public readonly workerFunction: lambda.Function;

  constructor(scope: Construct, id: string, props: AccountDeletionProps) {
    super(scope, id);
    const stack = cdk.Stack.of(this);

    // ADR: a dedicated schedule group | Rationale: CreateSchedule grants (scheduleGroup.grants.writeSchedules)
    // scope to schedules within this one group, never any other schedule in the account
    const scheduleGroup = new scheduler.ScheduleGroup(this, 'ScheduleGroup', {
      scheduleGroupName: 'cei-account-delete',
      removalPolicy: RemovalPolicy.DESTROY,
    });

    // ---------------------------------------------------------------
    // delete-worker Lambda: the scheduled target, runs the full cascade
    // ---------------------------------------------------------------

    const workerLogs = new logs.LogGroup(this, 'WorkerLogs', {
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: RemovalPolicy.DESTROY,
    });
    const workerRole = new iam.Role(this, 'WorkerRole', { assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com') });
    workerLogs.grantWrite(workerRole);
    // ADR: explicit functionName, ARN built with Stack.formatArn (not `this.workerFunction.functionArn`) |
    // Rationale: the worker's own CEI_DELETE_WORKER_ARN env var (self-reschedule target) would otherwise
    // make AccountDeletionWorkerFn's Properties reference its own Fn::GetAtt, an unresolvable CloudFormation
    // dependency cycle (confirmed by cdk-nag/template validation: "Circular dependency with
    // [AccountDeletionWorkerFn -> AccountDeletionWorkerFn]") | Alternative: Lazy.string rejected -- CDK
    // still resolves it to the same self-referencing Fn::GetAtt at synth time
    const workerFunctionName = 'cei-account-delete-worker';
    const workerArn = stack.formatArn({
      service: 'lambda', resource: 'function', resourceName: workerFunctionName,
      arnFormat: cdk.ArnFormat.COLON_RESOURCE_NAME,
    });
    this.workerFunction = new lambda.Function(this, 'WorkerFn', {
      functionName: workerFunctionName,
      tracing: lambda.Tracing.ACTIVE,
      runtime: lambda.Runtime.PYTHON_3_14,
      handler: 'handler.handle',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'lambdas', 'delete-worker'), {
        exclude: ['tests', '__pycache__', '*.pyc', '.pytest_cache'],
      }),
      layers: [props.pythonDepsLayer],
      role: workerRole,
      logGroup: workerLogs,
      // ADR: 14-minute timeout | Rationale: deep per-session cascades across many sessions self-reschedule
      // before this ceiling (handler.py SAFETY_MARGIN_MS), matching the Session Runner's own convention
      timeout: Duration.minutes(14),
      memorySize: 512,
      environment: {
        CEI_AWS_REGION: stack.region,
        CEI_USER_METADATA_TABLE: props.userMetadataTable.tableName,
        CEI_CHAT_METADATA_TABLE: props.chatMetadataTable.tableName,
        CEI_CHAT_HISTORY_TABLE: props.chatHistoryTable.tableName,
        CEI_ACTIVE_RUNS_TABLE: props.activeRunsTable.tableName,
        CEI_EXPORT_METADATA_TABLE: props.exportsTable.tableName,
        CEI_EXPORT_BUCKET: props.exportsBucket.bucketName,
        CEI_JOB_TABLE: props.jobTable.tableName,
        CEI_API_KEY_SECRET_PREFIX: props.apiKeySecretPrefix,
        CEI_API_KEY_SOURCE_IDS: props.apiKeySourceIds.join(','),
        CEI_MEMORY_ID: props.memoryId,
        CEI_SCHEDULE_GROUP_NAME: scheduleGroup.scheduleGroupName,
        USER_POOL_ID: props.userPool.userPoolId,
      },
    });
    suppressXrayWildcard(workerRole);

    // Scheduler execution role: EventBridge Scheduler assumes this only to invoke the worker. Both the
    // account-delete Lambda and the worker itself (self-reschedule) need iam:PassRole on it, below.
    const schedulerRole = new iam.Role(this, 'SchedulerRole', {
      assumedBy: new iam.ServicePrincipal('scheduler.amazonaws.com', {
        conditions: { StringEquals: { 'aws:SourceAccount': stack.account } },
      }),
    });
    this.workerFunction.grantInvoke(schedulerRole);
    NagSuppressions.addResourceSuppressions(schedulerRole, [{
      id: 'AwsSolutions-IAM5',
      reason: "ADR: Function.grantInvoke's own :* version/alias suffix | Rationale: grantInvoke always grants on both the function's bare ARN and <arn>:* so an alias/version invoke also works; this is CDK's own standard shape for Lambda invoke grants, not a custom wildcard | Alternative: a hand-rolled policy without the :* suffix rejected -- would diverge from the grant() helper for no security benefit",
    }], true);

    // Self-reschedule (early sweep guard, or nearing the 14-minute timeout): the worker needs the same
    // CreateSchedule/PassRole the account-delete Lambda has, scoped to the one group/role below.
    scheduleGroup.grants.writeSchedules(workerRole);
    schedulerRole.grantPassRole(workerRole);
    this.workerFunction.addEnvironment('CEI_DELETE_WORKER_ARN', workerArn);
    this.workerFunction.addEnvironment('CEI_SCHEDULER_ROLE_ARN', schedulerRole.roleArn);

    // ---------------------------------------------------------------
    // Worker IAM: scoped delete permissions for every store that exists in this stack today
    // ---------------------------------------------------------------

    // ChatMetadataStore has GSIs; Table.grant() would append "<table>/index/*" (IAM5) for a plain Query,
    // which this worker never needs (it queries the base table by user_id only) -- explicit statement.
    workerRole.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['dynamodb:Query', 'dynamodb:DeleteItem'], resources: [props.chatMetadataTable.tableArn],
    }));
    props.chatHistoryTable.grant(workerRole, 'dynamodb:Query', 'dynamodb:DeleteItem', 'dynamodb:BatchWriteItem');
    props.activeRunsTable.grant(workerRole, 'dynamodb:Query', 'dynamodb:DeleteItem');
    props.exportsTable.grant(workerRole, 'dynamodb:Query', 'dynamodb:DeleteItem');
    // JobIdStore (BackgroundJobs) also carries a status-updated_at GSI -- same IAM5 reasoning as above.
    workerRole.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['dynamodb:Query', 'dynamodb:DeleteItem'], resources: [props.jobTable.tableArn],
    }));
    workerRole.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:DeleteItem'], resources: [props.userMetadataTable.tableArn],
    }));
    workerRole.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['s3:DeleteObject'], resources: [props.exportsBucket.arnForObjects('exports/*')],
    }));
    // ADR: Secrets Manager delete scoped to the user-keys prefix, not per-secret ARNs | Rationale: F18
    // creates one secret per user per configured source after this stack deploys, so the exact secret
    // names do not exist at synth time; the worker loops the fixed source-id list (apiKeySourceIds)
    // instead of calling the broader secretsmanager:ListSecrets | Alternative: per-secret ARNs rejected
    // -- not knowable ahead of a user ever configuring a source (F18)
    workerRole.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['secretsmanager:DeleteSecret'],
      resources: [`arn:${stack.partition}:secretsmanager:${stack.region}:${stack.account}:secret:${props.apiKeySecretPrefix}/*`],
    }));
    workerRole.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: [
        'bedrock-agentcore:ListEvents', 'bedrock-agentcore:DeleteEvent',
        'bedrock-agentcore:ListMemoryRecords', 'bedrock-agentcore:BatchDeleteMemoryRecords',
      ],
      resources: [props.memoryArn],
    }));
    workerRole.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['cognito-idp:AdminDeleteUser'], resources: [props.userPool.userPoolArn],
    }));
    NagSuppressions.addResourceSuppressions(workerRole, [{
      id: 'AwsSolutions-IAM5',
      reason: 'ADR: three intentional prefix-scoped wildcards on this role | Rationale: (1) Secrets Manager delete scoped to cei/user-keys/* -- F18 creates one secret per user per configured source after this stack deploys, so exact secret names do not exist at synth time; (2) S3 DeleteObject scoped to exports/* -- the exact export object keys are generated at runtime (same pattern as exports-handler.ts); (3) scheduler:CreateSchedule/UpdateSchedule scoped to the one cei-account-delete schedule group (ScheduleGroupGrants.writeSchedules) for the worker\'s own self-reschedule | Alternative: exact ARNs rejected -- none of these three resources exist at synth time',
    }], true);

    // ---------------------------------------------------------------
    // account-delete Lambda: POST /account/delete
    // ---------------------------------------------------------------

    const deleteLogs = new logs.LogGroup(this, 'DeleteLogs', {
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: RemovalPolicy.DESTROY,
    });
    const deleteRole = new iam.Role(this, 'DeleteRole', { assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com') });
    deleteLogs.grantWrite(deleteRole);
    this.deleteFunction = new lambda.Function(this, 'DeleteFn', {
      tracing: lambda.Tracing.ACTIVE,
      runtime: lambda.Runtime.PYTHON_3_14,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'lambdas', 'account-delete'), {
        exclude: ['tests', '__pycache__', '*.pyc', '.pytest_cache'],
      }),
      layers: [props.pythonDepsLayer],
      role: deleteRole,
      logGroup: deleteLogs,
      timeout: Duration.seconds(20),
      memorySize: 256,
      environment: {
        CEI_AWS_REGION: stack.region,
        CEI_USER_METADATA_TABLE: props.userMetadataTable.tableName,
        USER_POOL_ID: props.userPool.userPoolId,
        SCHEDULE_GROUP_NAME: scheduleGroup.scheduleGroupName,
        SCHEDULER_ROLE_ARN: schedulerRole.roleArn,
        DELETE_WORKER_ARN: workerArn,
        ALLOWED_ORIGINS: props.allowedOrigins.join(','),
      },
    });
    suppressXrayWildcard(deleteRole);

    // Order (B109): the gate is written first (PutItem/GetItem), then Cognito is called, then the sweep
    // is scheduled -- the IAM grants below cover exactly those three steps, nothing else.
    deleteRole.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['dynamodb:PutItem', 'dynamodb:GetItem'], resources: [props.userMetadataTable.tableArn],
    }));
    deleteRole.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['cognito-idp:AdminDisableUser', 'cognito-idp:AdminUserGlobalSignOut'],
      resources: [props.userPool.userPoolArn],
    }));
    scheduleGroup.grants.writeSchedules(deleteRole);
    schedulerRole.grantPassRole(deleteRole);
    NagSuppressions.addResourceSuppressions(deleteRole, [{
      id: 'AwsSolutions-IAM5',
      reason: 'ADR: scheduler:CreateSchedule/UpdateSchedule scoped to the one cei-account-delete schedule group | Rationale: ScheduleGroupGrants.writeSchedules scopes to <group>/* because the per-user schedule name does not exist until a user actually requests deletion | Alternative: an exact schedule-name ARN rejected -- not knowable at synth time',
    }], true);

    // ---------------------------------------------------------------
    // POST /account/delete route (BUFFERED; Cognito authorizer, same as every other REST route)
    // ---------------------------------------------------------------

    const accountResource = props.api.root.addResource('account', { defaultCorsPreflightOptions: props.corsOptions });
    const deleteResource = accountResource.addResource('delete', { defaultCorsPreflightOptions: props.corsOptions });
    deleteResource.addMethod('POST', new apigw.LambdaIntegration(this.deleteFunction), props.methodOptions);
    for (const child of accountResource.node.findAll()) {
      if (child instanceof apigw.CfnMethod && child.httpMethod === 'OPTIONS') {
        child.requestValidatorId = props.requestValidator.requestValidatorId;
        NagSuppressions.addResourceSuppressions(child, [
          { id: 'AwsSolutions-APIG4', reason: 'ADR: CORS preflight without authorizer | Rationale: browsers do not send credentials on preflight | Alternative: authenticated preflight rejected because browsers cannot satisfy it' },
          { id: 'AwsSolutions-COG4', reason: 'ADR: CORS preflight without Cognito | Rationale: browsers do not send credentials on preflight | Alternative: authenticated preflight rejected because browsers cannot satisfy it' },
        ]);
      }
    }
  }
}
