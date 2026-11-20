import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Duration, RemovalPolicy } from 'aws-cdk-lib';
import * as apigw from 'aws-cdk-lib/aws-apigateway';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';
import { suppressXrayWildcard } from './tracing';

export interface ProfileProps {
  api: apigw.RestApi;
  methodOptions: apigw.MethodOptions;
  corsOptions: apigw.CorsOptions;
  requestValidator: apigw.IRequestValidator;
  pythonDepsLayer: lambda.ILayerVersion;
  allowedOrigins: string[];
  userPool: cognito.UserPool;
}

/**
 * B112: `POST /profile` -- one-time founder onboarding (first/last name, date of birth), stored ONLY as
 * standard Cognito user attributes (`given_name`, `family_name`, `birthdate`) via the Admin* API. No new
 * DynamoDB table, no pool replacement. `birthdate` is write-once (the Lambda itself enforces this by
 * reading the user's current attributes before writing); the SPA app client's `writeAttributes` stays
 * empty (backend-stack.ts), so this Lambda is the only writer.
 *
 * ADR: a one-directional IAM reference only | Rationale: this Lambda's role is granted Cognito Admin*
 * actions scoped to `userPool.userPoolArn`; the pool itself never references this Lambda (it is not a
 * `lambdaTriggers` entry), so there is no CloudFormation cycle.
 */
export class Profile extends Construct {
  public readonly function: lambda.Function;

  constructor(scope: Construct, id: string, props: ProfileProps) {
    super(scope, id);
    const stack = cdk.Stack.of(this);

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
      code: lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'lambdas', 'profile-handler'), {
        exclude: ['tests', '__pycache__', '*.pyc', '.pytest_cache'],
      }),
      layers: [props.pythonDepsLayer],
      role,
      logGroup,
      timeout: Duration.seconds(10),
      memorySize: 256,
      environment: {
        CEI_AWS_REGION: stack.region,
        USER_POOL_ID: props.userPool.userPoolId,
        ALLOWED_ORIGINS: props.allowedOrigins.join(','),
      },
    });
    // IAM: exactly the four Admin* calls the handler makes, scoped to the one user pool ARN (B112 work item 1).
    role.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: [
        'cognito-idp:ListUsers',
        'cognito-idp:AdminUpdateUserAttributes',
        'cognito-idp:AdminDisableUser',
        'cognito-idp:AdminUserGlobalSignOut',
      ],
      resources: [props.userPool.userPoolArn],
    }));
    suppressXrayWildcard(role);

    const integration = new apigw.LambdaIntegration(this.function);
    const profileResource = props.api.root.addResource('profile', { defaultCorsPreflightOptions: props.corsOptions });
    profileResource.addMethod('POST', integration, props.methodOptions);

    for (const child of profileResource.node.findAll()) {
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
