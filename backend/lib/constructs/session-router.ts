import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import { Construct } from 'constructs';

export interface SessionRouterProps {
  role: iam.Role;
  pythonDepsLayer: lambda.ILayerVersion;
  metadataTable: dynamodb.ITable;
  activeRunsTable: dynamodb.ITable;
  userPoolId: string;
  spaClientId: string;
  tokenEndpoint: string;
  clientId: string;
  secretArn: string;
  scopes: string[];
  runtimeUrl: string;
}

/** The Router has no public URL; the Runner alone must receive invoke permission. */
export class SessionRouter extends Construct {
  public readonly function: lambda.Function;

  constructor(scope: Construct, id: string, props: SessionRouterProps) {
    super(scope, id);
    const logGroup = new logs.LogGroup(this, 'Logs', { retention: logs.RetentionDays.ONE_MONTH, removalPolicy: cdk.RemovalPolicy.DESTROY });
    logGroup.grantWrite(props.role);
    props.role.addToPolicy(new iam.PolicyStatement({ actions: ['dynamodb:GetItem'], resources: [props.metadataTable.tableArn] }));
    props.role.addToPolicy(new iam.PolicyStatement({ actions: ['dynamodb:GetItem', 'dynamodb:UpdateItem'], resources: [props.activeRunsTable.tableArn] }));
    this.function = new lambda.Function(this, 'Function', {
      runtime: lambda.Runtime.PYTHON_3_14,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'lambdas', 'session-router')),
      role: props.role,
      logGroup,
      layers: [props.pythonDepsLayer],
      timeout: cdk.Duration.seconds(30),
      environment: {
        USER_POOL_ID: props.userPoolId,
        SPA_CLIENT_ID: props.spaClientId,
        METADATA_TABLE: props.metadataTable.tableName,
        ACTIVE_RUNS_TABLE: props.activeRunsTable.tableName,
        ROUTER_CLIENT_ID: props.clientId,
        ROUTER_SECRET_ARN: props.secretArn,
        TOKEN_ENDPOINT: props.tokenEndpoint,
        TOOL_SCOPES: props.scopes.join(','),
        RESEARCH_RUNTIME_URL: props.runtimeUrl,
      },
    });
  }
}
