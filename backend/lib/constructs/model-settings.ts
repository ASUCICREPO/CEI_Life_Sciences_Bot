import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Duration, RemovalPolicy } from 'aws-cdk-lib';
import * as apigw from 'aws-cdk-lib/aws-apigateway';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';
import { suppressXrayWildcard } from './tracing';

export interface ModelSettingsProps {
  api: apigw.RestApi;
  methodOptions: apigw.MethodOptions;
  corsOptions: apigw.CorsOptions;
  requestValidator: apigw.IRequestValidator;
  pythonDepsLayer: lambda.ILayerVersion;
  allowedOrigins: string[];
  /** B18 blocklist, merged with the fixed D3 defaults by shared/config.py's model_blocklist property. */
  blockedModelIds: string[];
}

/**
 * F8: `GET /models` (blocklist-filtered Bedrock model discovery) and `GET`/`PUT /settings`
 * (`default_model_id`/`default_mode`), backed by the UserMetadataTable (data-model.md Section 4 —
 * its first real use). One Lambda, one table; both routes share the same handler module.
 */
export class ModelSettings extends Construct {
  public readonly table: dynamodb.Table;
  public readonly function: lambda.Function;

  constructor(scope: Construct, id: string, props: ModelSettingsProps) {
    super(scope, id);
    const stack = cdk.Stack.of(this);

    this.table = new dynamodb.Table(this, 'UserMetadataTable', {
      partitionKey: { name: 'user_id', type: dynamodb.AttributeType.STRING },
      // ADR: fixed sort key value "profile" | Rationale: data-model.md Section 4 — single-item-per-user table,
      // no natural range key; a fixed SK value leaves room for other per-user record kinds later without a
      // table redesign | Alternative: no sort key rejected — keeps this table's key shape consistent with the
      // other per-user tables (ActiveRunsStore) for the same reason
      sortKey: { name: 'record', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      // ADR: RETAIN | Rationale: user settings are user data — a stack teardown must not silently delete them
      removalPolicy: RemovalPolicy.RETAIN,
    });

    const logGroup = new logs.LogGroup(this, 'Logs', {
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: RemovalPolicy.DESTROY,
    });
    // ADR: hand-rolled role, no AWSLambdaBasicExecutionRole | Rationale: same as the other session Lambdas — avoids IAM4
    const role = new iam.Role(this, 'Role', { assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com') });
    logGroup.grantWrite(role);
    this.function = new lambda.Function(this, 'Fn', {
      tracing: lambda.Tracing.ACTIVE,
      runtime: lambda.Runtime.PYTHON_3_14,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'lambdas', 'models-settings-handler'), {
        exclude: ['tests', '__pycache__', '*.pyc', '.pytest_cache'],
      }),
      layers: [props.pythonDepsLayer],
      role,
      logGroup,
      timeout: Duration.seconds(10),
      memorySize: 256,
      environment: {
        CEI_AWS_REGION: stack.region,
        CEI_USER_METADATA_TABLE: this.table.tableName,
        CEI_BLOCKED_MODELS: props.blockedModelIds.join(','),
        ALLOWED_ORIGINS: props.allowedOrigins.join(','),
      },
    });
    role.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['dynamodb:GetItem', 'dynamodb:PutItem'], resources: [this.table.tableArn],
    }));
    // ADR: resource "*" for Bedrock model discovery | Rationale: ListFoundationModels/ListInferenceProfiles are
    // account+Region-wide list operations with no resource-level ARN to scope to; GetInferenceProfile resolves
    // an inference profile named only in the list response, not known at synth time | Alternative: per-model
    // ARNs rejected — these are read-only discovery calls, not model invocation (which stays Anthropic-only,
    // agent-runtimes.ts)
    role.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['bedrock:ListFoundationModels', 'bedrock:ListInferenceProfiles', 'bedrock:GetInferenceProfile'],
      resources: ['*'],
    }));
    NagSuppressions.addResourceSuppressions(role, [{
      id: 'AwsSolutions-IAM5',
      reason: 'ADR: Bedrock discovery actions have no resource-level ARN | Rationale: ListFoundationModels/ListInferenceProfiles/GetInferenceProfile are account+Region-wide read-only list/describe operations; AWS defines no resource type to scope them to | Alternative: per-model ARNs rejected — not applicable to list operations',
    }], true);
    suppressXrayWildcard(role);

    const integration = new apigw.LambdaIntegration(this.function);
    const modelsResource = props.api.root.addResource('models', { defaultCorsPreflightOptions: props.corsOptions });
    modelsResource.addMethod('GET', integration, props.methodOptions);
    const settingsResource = props.api.root.addResource('settings', { defaultCorsPreflightOptions: props.corsOptions });
    settingsResource.addMethod('GET', integration, props.methodOptions);
    settingsResource.addMethod('PUT', integration, props.methodOptions);

    for (const resource of [modelsResource, settingsResource]) {
      for (const child of resource.node.findAll()) {
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
}
