// FIXME: remove after S1 validation (B92).
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { Construct } from 'constructs';

/** Isolated transport experiment; never included in a normal deployment. */
export class StreamingSpikeStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);
    const pool = new cognito.UserPool(this, 'Users', { removalPolicy: cdk.RemovalPolicy.DESTROY });
    const client = pool.addClient('TestClient', { authFlows: { userPassword: true }, generateSecret: false });
    const runner = new lambda.Function(this, 'Runner', {
      runtime: lambda.Runtime.NODEJS_22_X,
      handler: 'index.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'lambdas', 'spike-streaming-runner')),
      timeout: cdk.Duration.minutes(14),
    });
    const api = new apigateway.RestApi(this, 'Api', { endpointTypes: [apigateway.EndpointType.REGIONAL] });
    const authorizer = new apigateway.CognitoUserPoolsAuthorizer(this, 'Authorizer', { cognitoUserPools: [pool] });
    api.root.addResource('stream').addMethod('POST', new apigateway.LambdaIntegration(runner, {
      responseTransferMode: apigateway.ResponseTransferMode.STREAM,
      timeout: cdk.Duration.minutes(14),
    }), { authorizer, authorizationType: apigateway.AuthorizationType.COGNITO });
    const distribution = new cloudfront.Distribution(this, 'Distribution', {
      defaultBehavior: {
        origin: new origins.HttpOrigin(`${api.restApiId}.execute-api.${this.region}.${this.urlSuffix}`, { originPath: `/${api.deploymentStage.stageName}` }),
        allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
        cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
        originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
        compress: false,
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
      },
    });
    new cdk.CfnOutput(this, 'StreamUrl', { value: `https://${distribution.distributionDomainName}/stream` });
    new cdk.CfnOutput(this, 'UserPoolId', { value: pool.userPoolId });
    new cdk.CfnOutput(this, 'UserPoolClientId', { value: client.userPoolClientId });
  }
}
