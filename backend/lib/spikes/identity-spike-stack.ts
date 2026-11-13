import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { aws_bedrockagentcore as agentcore, aws_cognito as cognito, aws_iam as iam,
  aws_lambda as lambda, aws_logs as logs, aws_secretsmanager as secretsmanager } from 'aws-cdk-lib';
import { AwsSolutionsChecks, NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';
import { PythonDepsLayer } from '../constructs/python-deps-layer';
import { suppressXrayWildcard } from '../constructs/tracing';
import { cedarPermit } from '../constructs/cedar-policy';

/** Isolated synthetic B99 identity proof. Never attach this target to an application Gateway. */
export class IdentitySpikeStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: cdk.StackProps = {}) {
    super(scope, id, props);
    cdk.Aspects.of(this).add(new AwsSolutionsChecks({ verbose: true }));
    cdk.Tags.of(this).add('Project', 'CEI-Life-Sciences-Bot');
    cdk.Tags.of(this).add('Spike', 'B99-identity');
    const layer = new PythonDepsLayer(this, 'Deps');
    const makeFn = (name: string, dir: string, handler: string, env: Record<string, string> = {}, withLayer = false) => {
      const role = new iam.Role(this, `${name}Role`, { assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com') });
      const group = new logs.LogGroup(this, `${name}Logs`, { retention: logs.RetentionDays.ONE_WEEK, removalPolicy: cdk.RemovalPolicy.DESTROY });
      group.grantWrite(role);
      const fn = new lambda.Function(this, name, {
        runtime: lambda.Runtime.PYTHON_3_14, handler,
        code: lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'lambdas', dir),
          { exclude: ['test*', '__pycache__', '*.pyc'] }),
        role, logGroup: group, environment: env, timeout: cdk.Duration.seconds(15),
        layers: withLayer ? [layer.layer] : [], tracing: lambda.Tracing.ACTIVE,
      });
      suppressXrayWildcard(role);
      return fn;
    };
    const trigger = makeFn('PreToken', 'pre-token-generation', 'handler.handler');
    const pool = new cognito.UserPool(this, 'Pool', {
      selfSignUpEnabled: false, featurePlan: cognito.FeaturePlan.ESSENTIALS,
      passwordPolicy: { minLength: 12, requireLowercase: true, requireUppercase: true, requireDigits: true, requireSymbols: true },
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    NagSuppressions.addResourceSuppressions(pool, [{ id: 'AwsSolutions-COG8',
      reason: 'ADR: Synthetic spike pool | Rationale: this isolated pool issues only short-lived machine tokens and has no interactive users | Alternative: Plus tier rejected for a temporary synthetic probe',
    }], true);
    pool.addTrigger(cognito.UserPoolOperation.PRE_TOKEN_GENERATION_CONFIG, trigger, cognito.LambdaVersion.V3_0);
    const server = pool.addResourceServer('Scopes', {
      identifier: 'cei-spike', scopes: [new cognito.ResourceServerScope({ scopeName: 'probe', scopeDescription: 'Synthetic identity probe' })],
    });
    const client = pool.addClient('Client', {
      generateSecret: true, accessTokenValidity: cdk.Duration.minutes(5),
      oAuth: { flows: { clientCredentials: true }, scopes: [cognito.OAuthScope.resourceServer(server,
        new cognito.ResourceServerScope({ scopeName: 'probe', scopeDescription: 'Synthetic identity probe' }))] },
    });
    (client.node.defaultChild as cognito.CfnUserPoolClient).explicitAuthFlows = ['ALLOW_REFRESH_TOKEN_AUTH'];
    const domain = pool.addDomain('Domain', { cognitoDomain: { domainPrefix: `cei-identity-spike-${cdk.Aws.ACCOUNT_ID}` } });
    const secret = new secretsmanager.Secret(this, 'ClientSecret', {
      secretStringValue: cdk.SecretValue.resourceAttribute((client.node.defaultChild as cognito.CfnUserPoolClient).attrClientSecret),
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    NagSuppressions.addResourceSuppressions(secret, [{ id: 'AwsSolutions-SMG4',
      reason: 'ADR: Temporary spike credential | Rationale: the secret mirrors a short-lived synthetic app client and is removed with the isolated stack | Alternative: scheduled rotation rejected for a temporary test resource',
    }]);
    const interceptor = makeFn('Interceptor', 'spikes/b99_identity', 'interceptor.handler', {
      USER_POOL_ID: pool.userPoolId, ROUTER_CLIENT_ID: client.userPoolClientId, SPIKE_SCOPE: 'cei-spike/probe',
    }, true);
    const targetFn = makeFn('Target', 'spikes/b99_identity', 'target.handler');
    const probeCaller = new iam.Role(this, 'ProbeCallerRole', {
      assumedBy: new iam.AccountPrincipal(this.account),
      description: 'Synthetic runtime principal used to verify direct invocation denial',
    });
    probeCaller.addToPolicy(new iam.PolicyStatement({ effect: iam.Effect.DENY,
      actions: ['lambda:InvokeFunction'], resources: [targetFn.functionArn] }));
    const engine = new agentcore.CfnPolicyEngine(this, 'Engine', { name: 'cei_identity_spike_engine' });
    const role = new iam.Role(this, 'GatewayRole', { assumedBy: new iam.PrincipalWithConditions(
      new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'), {
        StringEquals: { 'aws:SourceAccount': this.account },
        ArnLike: { 'aws:SourceArn': this.formatArn({ service: 'bedrock-agentcore', resource: '*', arnFormat: cdk.ArnFormat.NO_RESOURCE_NAME }) },
      }) });
    role.addToPolicy(new iam.PolicyStatement({ actions: ['lambda:InvokeFunction'], resources: [interceptor.functionArn, targetFn.functionArn] }));
    role.addToPolicy(new iam.PolicyStatement({ actions: ['bedrock-agentcore:GetPolicyEngine'], resources: [engine.attrPolicyEngineArn] }));
    role.addToPolicy(new iam.PolicyStatement({ actions: ['bedrock-agentcore:AuthorizeAction', 'bedrock-agentcore:PartiallyAuthorizeActions'],
      resources: [engine.attrPolicyEngineArn, this.formatArn({ service: 'bedrock-agentcore', resource: 'gateway', resourceName: '*' })] }));
    NagSuppressions.addResourceSuppressions(role, [{ id: 'AwsSolutions-IAM5',
      reason: 'ADR: Spike Gateway authorization scope | Rationale: Gateway ARN includes a service-generated suffix and cannot be referenced before the role exists | Alternative: fixed ARN rejected because it creates a resource cycle',
    }], true);
    const gateway = new agentcore.CfnGateway(this, 'Gateway', {
      name: 'cei-identity-spike', protocolType: 'MCP', roleArn: role.roleArn, authorizerType: 'CUSTOM_JWT',
      authorizerConfiguration: { customJwtAuthorizer: {
        discoveryUrl: `https://cognito-idp.${this.region}.amazonaws.com/${pool.userPoolId}/.well-known/openid-configuration`,
        allowedClients: [client.userPoolClientId],
      } },
      interceptorConfigurations: [{ interceptionPoints: ['REQUEST'], interceptor: { lambda: { arn: interceptor.functionArn } },
        inputConfiguration: { passRequestHeaders: true } }],
      policyEngineConfiguration: { arn: engine.attrPolicyEngineArn, mode: 'ENFORCE' },
    });
    gateway.node.addDependency(role);
    const target = new agentcore.CfnGatewayTarget(this, 'GatewayTarget', {
      name: 'identity-probe', gatewayIdentifier: gateway.attrGatewayIdentifier,
      credentialProviderConfigurations: [{ credentialProviderType: 'GATEWAY_IAM_ROLE' }],
      targetConfiguration: { mcp: { lambda: { lambdaArn: targetFn.functionArn, toolSchema: { inlinePayload: [{
        name: 'probe', description: 'Synthetic identity transport probe', inputSchema: {
          type: 'object', properties: { query: { type: 'string' }, _cei_spike_identity: { type: 'object',
            properties: { user_id: { type: 'string' }, session_id: { type: 'string' } } } }, required: ['query'],
        },
      }] } } } },
    });
    target.node.addDependency(role);
    const policy = new agentcore.CfnPolicy(this, 'Permit', {
      name: 'permit_identity_probe', policyEngineId: engine.attrPolicyEngineId,
      definition: { cedar: { statement: cedarPermit('identity-probe', gateway.attrGatewayArn, 'cei-spike/probe') } },
      validationMode: 'IGNORE_ALL_FINDINGS',
    });
    policy.node.addDependency(target);
    for (const [name, value] of Object.entries({ GatewayUrl: gateway.attrGatewayUrl, UserPoolId: pool.userPoolId,
      ClientId: client.userPoolClientId, TokenEndpoint: `${domain.baseUrl()}/oauth2/token`, ClientSecretArn: secret.secretArn,
      TargetArn: targetFn.functionArn, GatewayRoleArn: role.roleArn, ProbeCallerRoleArn: probeCaller.roleArn })) {
      new cdk.CfnOutput(this, name, { value });
    }
  }
}
