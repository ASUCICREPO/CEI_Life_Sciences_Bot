// FIXME: remove after S2 validation (B92)
// Throwaway S2 spike stack: proves Policy in AgentCore (Cedar) can scope tools per Cognito M2M token.
// Synthesized only when `-c ceiSpikes=tool-scope` is set (see app.ts). Destroy it after the findings.
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Aspects, Duration, RemovalPolicy } from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { AwsSolutionsChecks, NagSuppressions } from 'cdk-nag';
import { aws_bedrockagentcore as agentcore } from 'aws-cdk-lib';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';

export type SpikePolicyMode = 'LOG_ONLY' | 'ENFORCE';

export interface ToolScopeSpikeStackProps extends cdk.StackProps {
  /** Policy engine mode on the Gateway. Defaults to LOG_ONLY (B30: start LOG_ONLY, then ENFORCE). */
  policyMode?: string;
}

/** Resource server identifier; full scope names are `cei-tools-spike/<scope>`. */
const RESOURCE_SERVER_ID = 'cei-tools-spike';

// ADR: no scope name is a substring of another | Rationale: B69 / auth-and-tool-scope.md section 3 — Cedar `like "*scope*"` is a substring match, so `target-a` must not sit inside any other scope; `probe` is a fourth scope no policy references, so a "token with no relevant scope" is possible
const SCOPES = {
  targetA: 'target-a',
  targetB: 'target-b',
  shared: 'shared',
  probe: 'probe',
};

/** Tool name every echo target exposes; the Gateway prefixes it: `<TargetName>___echo`. */
const TOOL_NAME = 'echo';

interface EchoTargetSpec {
  /** Gateway target name = Cedar action group name. CamelCase: Cedar action ids are `<Target>___<tool>`. */
  targetName: string;
  /** Lambda asset directory under backend/lambdas. */
  dir: string;
  /** Scope that permits this target, or undefined when every caller is permitted (B70). */
  scope?: string;
}

const ECHO_TARGETS: EchoTargetSpec[] = [
  { targetName: 'SpikeTargetA', dir: 'spike-echo-target-a', scope: SCOPES.targetA },
  { targetName: 'SpikeTargetB', dir: 'spike-echo-target-b', scope: SCOPES.targetB },
  { targetName: 'SpikeTargetShared', dir: 'spike-echo-target-shared' },
];

export class ToolScopeSpikeStack extends cdk.Stack {
  public readonly policyMode: SpikePolicyMode;

  constructor(scope: Construct, id: string, props: ToolScopeSpikeStackProps = {}) {
    super(scope, id, props);

    const mode = props.policyMode ?? 'LOG_ONLY';
    if (mode !== 'LOG_ONLY' && mode !== 'ENFORCE') {
      throw new Error(`ceiSpikePolicyMode must be LOG_ONLY or ENFORCE, got '${mode}'.`);
    }
    this.policyMode = mode;

    // ADR: cdk-nag scoped to stack, not app entry point | Rationale: same convention as BackendStack | Alternative: Aspects.of(app) rejected
    Aspects.of(this).add(new AwsSolutionsChecks({ verbose: true }));
    cdk.Tags.of(this).add('Project', 'CEI-Life-Sciences-Bot');
    cdk.Tags.of(this).add('Spike', 'S2-tool-scope');

    // ---------------------------------------------------------------
    // Cognito: resource server + M2M client (client credentials)
    // ---------------------------------------------------------------

    const userPool = new cognito.UserPool(this, 'SpikeUserPool', {
      selfSignUpEnabled: false,
      mfa: cognito.Mfa.OFF,
      passwordPolicy: { minLength: 12, requireLowercase: true, requireUppercase: true, requireDigits: true, requireSymbols: true },
      removalPolicy: RemovalPolicy.DESTROY,
    });

    const resourceServer = userPool.addResourceServer('SpikeResourceServer', {
      identifier: RESOURCE_SERVER_ID,
      scopes: Object.values(SCOPES).map((scopeName) => new cognito.ResourceServerScope({ scopeName, scopeDescription: `S2 spike scope ${scopeName}` })),
    });
    const oauthScope = (name: string) => cognito.OAuthScope.resourceServer(resourceServer, new cognito.ResourceServerScope({ scopeName: name, scopeDescription: name }));

    const m2mClient = userPool.addClient('SpikeM2MClient', {
      generateSecret: true,
      oAuth: {
        flows: { clientCredentials: true },
        scopes: Object.values(SCOPES).map(oauthScope),
      },
      // ADR: 5 min access-token TTL | Rationale: Cognito's minimum; lets the script watch a token expire mid-run (task 9) without an hour-long wait | Alternative: 1 h per B67 rejected for the spike only
      accessTokenValidity: Duration.minutes(5),
    });

    // ADR: domain prefix from the account id | Rationale: Cognito domain prefixes are unique per Region, and the account id keeps it stable without a random suffix
    const domain = userPool.addDomain('SpikeDomain', {
      cognitoDomain: { domainPrefix: cdk.Fn.join('-', ['cei-spike', cdk.Aws.ACCOUNT_ID]) },
    });
    const tokenEndpoint = `${domain.baseUrl()}/oauth2/token`;

    // ---------------------------------------------------------------
    // Roles: runner (alone can read the M2M secret) vs runtime (must not)
    // ---------------------------------------------------------------

    // ADR: trust the account root, not a named user | Rationale: any IAM identity in the account that holds sts:AssumeRole (the CloudShell user) can assume it; the secret's resource policy, not the trust, is the boundary under test | Alternative: a hard-coded user ARN rejected — unknown at synth time
    // The Lambda service principal is also trusted: the token-refresh stub runs AS the runner role (B30/B67: only the Router and the Token Refresh Lambda hold the secret).
    const runnerRole = new iam.Role(this, 'SpikeRunnerRole', {
      roleName: `cei-spike-runner-${this.account}`,
      assumedBy: new iam.CompositePrincipal(new iam.AccountPrincipal(this.account), new iam.ServicePrincipal('lambda.amazonaws.com')),
      description: 'S2 spike: Router-equivalent. The only identity allowed to read the M2M client secret.',
    });
    const runtimeRole = new iam.Role(this, 'SpikeRuntimeRole', {
      roleName: `cei-spike-runtime-${this.account}`,
      assumedBy: new iam.AccountPrincipal(this.account),
      description: 'S2 spike: AgentCore-runtime-equivalent. Must NOT be able to read the M2M client secret.',
    });
    // ADR: deliberately over-broad identity policy on the runtime role | Rationale: proves the secret's explicit resource-policy Deny beats an identity Allow, not just the absence of a grant | Alternative: no identity policy rejected — would only show an implicit deny
    runtimeRole.addToPolicy(new iam.PolicyStatement({ actions: ['secretsmanager:GetSecretValue'], resources: ['*'] }));

    const clientSecret = new secretsmanager.Secret(this, 'SpikeM2MSecret', {
      description: 'S2 spike: Cognito M2M app client secret',
      // ADR: client secret via the L1 ClientSecret attribute | Rationale: L2 userPoolClientSecret adds an AwsCustomResource (describeUserPoolClient) with a CDK-owned AWSLambdaBasicExecutionRole role (IAM4) | Alternative: suppressing that singleton rejected � avoidable
      secretStringValue: cdk.SecretValue.resourceAttribute((m2mClient.node.defaultChild as cognito.CfnUserPoolClient).attrClientSecret),
      removalPolicy: RemovalPolicy.DESTROY,
    });
    clientSecret.grantRead(runnerRole);
    // Deny GetSecretValue to everyone except the runner role (checked against the role ARN, which is what aws:PrincipalArn holds for assumed-role sessions).
    clientSecret.addToResourcePolicy(
      new iam.PolicyStatement({
        sid: 'DenyAllButRunner',
        effect: iam.Effect.DENY,
        principals: [new iam.AnyPrincipal()],
        actions: ['secretsmanager:GetSecretValue'],
        resources: ['*'],
        conditions: { StringNotEquals: { 'aws:PrincipalArn': runnerRole.roleArn } },
      }),
    );

    // ---------------------------------------------------------------
    // Echo Lambdas (Gateway targets)
    // ---------------------------------------------------------------

    const lambdaAsset = (dir: string) =>
      lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'lambdas', dir), { exclude: ['tests', '__pycache__', '*.pyc'] });

    const makeFunction = (fnId: string, dir: string, role?: iam.Role, environment?: Record<string, string>) => {
      const logGroup = new logs.LogGroup(this, `${fnId}Logs`, { retention: logs.RetentionDays.ONE_WEEK, removalPolicy: RemovalPolicy.DESTROY });
      // ADR: hand-rolled role, no AWSLambdaBasicExecutionRole | Rationale: same as BackendStack — avoids IAM4; grantWrite scopes logs to this log group
      const fnRole = role ?? new iam.Role(this, `${fnId}Role`, { assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com') });
      logGroup.grantWrite(fnRole);
      return new lambda.Function(this, fnId, {
        runtime: lambda.Runtime.PYTHON_3_14,
        handler: 'handler.handler',
        code: lambdaAsset(dir),
        role: fnRole,
        logGroup,
        timeout: Duration.seconds(15),
        environment,
      });
    };

    const echoFunctions = ECHO_TARGETS.map((spec) => ({ spec, fn: makeFunction(`${spec.targetName}Fn`, spec.dir) }));

    // ---------------------------------------------------------------
    // Gateway (custom JWT authorizer on the spike pool) + policy engine
    // ---------------------------------------------------------------

    const policyEngine = new agentcore.CfnPolicyEngine(this, 'SpikePolicyEngine', {
      // Policy engine names: [A-Za-z][A-Za-z0-9_]*, max 48 (no hyphens).
      name: 'cei_tools_spike_engine',
      description: 'S2 spike: per-target Cedar permits, default deny',
    });

    const gatewayRole = new iam.Role(this, 'SpikeGatewayRole', {
      assumedBy: new iam.PrincipalWithConditions(new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'), {
        StringEquals: { 'aws:SourceAccount': this.account },
        ArnLike: { 'aws:SourceArn': this.formatArn({ service: 'bedrock-agentcore', resource: '*', arnFormat: cdk.ArnFormat.NO_RESOURCE_NAME }) },
      }),
    });
    gatewayRole.addToPolicy(new iam.PolicyStatement({ actions: ['lambda:InvokeFunction'], resources: echoFunctions.map(({ fn }) => fn.functionArn) }));
    // GetPolicyEngine / AuthorizeAction / PartiallyAuthorizeActions are required for Policy; PartiallyAuthorizeActions is what filters tools/list.
    gatewayRole.addToPolicy(new iam.PolicyStatement({ actions: ['bedrock-agentcore:GetPolicyEngine'], resources: [policyEngine.attrPolicyEngineArn] }));
    // ADR: gateway/* wildcard in the gateway role | Rationale: the role must exist before the gateway (it is a required property), so the gateway ARN cannot be referenced without a circular dependency | Alternative: fixed gateway name in the ARN rejected — the service appends a random suffix
    gatewayRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ['bedrock-agentcore:AuthorizeAction', 'bedrock-agentcore:PartiallyAuthorizeActions'],
        resources: [policyEngine.attrPolicyEngineArn, this.formatArn({ service: 'bedrock-agentcore', resource: 'gateway', resourceName: '*' })],
      }),
    );

    const gateway = new agentcore.CfnGateway(this, 'SpikeGateway', {
      name: 'cei-tools-spike',
      description: 'S2 spike gateway',
      protocolType: 'MCP',
      authorizerType: 'CUSTOM_JWT',
      authorizerConfiguration: {
        customJwtAuthorizer: {
          discoveryUrl: `https://cognito-idp.${this.region}.amazonaws.com/${userPool.userPoolId}/.well-known/openid-configuration`,
          // ADR: allowedClients, not allowedAudience/allowedScopes | Rationale: Cognito client-credentials access tokens carry `client_id`, not `aud`; no allowedScopes so a token with only an unreferenced scope still reaches the Gateway and the policy engine decides | Alternative: allowedScopes rejected — would reject before Cedar runs, hiding the deny shape
          allowedClients: [m2mClient.userPoolClientId],
        },
      },
      roleArn: gatewayRole.roleArn,
      // ADR: exceptionLevel DEBUG | Rationale: task 8 needs the raw deny error, not a generic message | Alternative: default rejected — hides detail
      exceptionLevel: 'DEBUG',
      policyEngineConfiguration: { arn: policyEngine.attrPolicyEngineArn, mode },
    });
    gateway.node.addDependency(gatewayRole);

    const targets = echoFunctions.map(({ spec, fn }) => {
      const target = new agentcore.CfnGatewayTarget(this, `${spec.targetName}Target`, {
        name: spec.targetName,
        description: `S2 spike echo target (${spec.dir})`,
        gatewayIdentifier: gateway.attrGatewayIdentifier,
        credentialProviderConfigurations: [{ credentialProviderType: 'GATEWAY_IAM_ROLE' }],
        targetConfiguration: {
          mcp: {
            lambda: {
              lambdaArn: fn.functionArn,
              toolSchema: {
                inlinePayload: [
                  {
                    name: TOOL_NAME,
                    description: `Echo the input (${spec.dir})`,
                    inputSchema: { type: 'object', properties: { message: { type: 'string', description: 'Text to echo' } }, required: ['message'] },
                  },
                ],
              },
            },
          },
        },
      });
      // The role policy must exist before the Gateway validates the Lambda target.
      target.node.addDependency(gatewayRole);
      return { spec, target };
    });

    // B69: ONE permit per target (`action in AgentCore::Action::"<Target>"` covers every tool of that target).
    // The `scope` claim is a space-separated string tag; `like "*scope*"` is a substring match (B69 collision rule).
    // B70: the shared target has NO scope condition, so every authenticated caller reaches it by default.
    // ADR: no permit for initialize / tools/list | Rationale: the GA Cedar schema only has tool actions (one per `<Target>___<tool>`), so there is nothing to permit; tools/list is filtered through PartiallyAuthorizeActions | Alternative: a blanket `forbid` for default deny rejected — Cedar is already default deny and a forbid overrides every permit
    const policies = targets.map(({ spec, target }) => {
      const scopeCondition = spec.scope
        ? ` when { principal.hasTag("scope") && principal.getTag("scope") like "*${RESOURCE_SERVER_ID}/${spec.scope}*" }`
        : '';
      const statement = `permit(principal is AgentCore::OAuthUser, action in AgentCore::Action::"${spec.targetName}", resource == AgentCore::Gateway::"${gateway.attrGatewayArn}")${scopeCondition};`;
      const policy = new agentcore.CfnPolicy(this, `Permit${spec.targetName}`, {
        name: `permit_${spec.targetName}`,
        policyEngineId: policyEngine.attrPolicyEngineId,
        description: spec.scope ? `Permit ${spec.targetName} when the token carries ${spec.scope}` : `Permit ${spec.targetName} for every caller (B70)`,
        definition: { cedar: { statement } },
        // ADR: IGNORE_ALL_FINDINGS | Rationale: the shared permit is deliberately open to every caller (B70), which the Cedar analyzer reports as overly permissive | Alternative: FAIL_ON_ANY_FINDINGS rejected — would fail the deploy on the intended shape
        validationMode: 'IGNORE_ALL_FINDINGS',
      });
      // CreatePolicy validates the action against the Gateway's tool schema, so the target must exist first.
      policy.node.addDependency(target);
      policy.node.addDependency(gateway);
      return policy;
    });

    // LOG_ONLY denies are only visible in the Gateway's APPLICATION_LOGS (vended logs delivery).
    const gatewayLogGroup = new logs.LogGroup(this, 'SpikeGatewayLogs', {
      logGroupName: `/aws/vendedlogs/bedrock-agentcore/gateway/${this.stackName}`,
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: RemovalPolicy.DESTROY,
    });
    const deliverySource = new logs.CfnDeliverySource(this, 'SpikeGatewayLogSource', {
      name: `${this.stackName}-gateway-logs`,
      logType: 'APPLICATION_LOGS',
      resourceArn: gateway.attrGatewayArn,
    });
    const deliveryDestination = new logs.CfnDeliveryDestination(this, 'SpikeGatewayLogDestination', {
      name: `${this.stackName}-gateway-logs-dest`,
      deliveryDestinationType: 'CWL',
      destinationResourceArn: gatewayLogGroup.logGroupArn,
    });
    const delivery = new logs.CfnDelivery(this, 'SpikeGatewayLogDelivery', {
      deliverySourceName: deliverySource.name,
      deliveryDestinationArn: deliveryDestination.attrArn,
    });
    delivery.node.addDependency(deliverySource);
    delivery.node.addDependency(deliveryDestination);

    // ---------------------------------------------------------------
    // Stub token-refresh endpoint (B67): IAM-authenticated Function URL
    // ---------------------------------------------------------------

    const refreshFn = makeFunction('TokenRefreshStubFn', 'spike-token-refresh-stub', runnerRole, {
      SECRET_ARN: clientSecret.secretArn,
      CLIENT_ID: m2mClient.userPoolClientId,
      TOKEN_ENDPOINT: tokenEndpoint,
    });
    const refreshUrl = refreshFn.addFunctionUrl({ authType: lambda.FunctionUrlAuthType.AWS_IAM });
    // Since Oct 2025 a Function URL with AWS_IAM needs BOTH actions on the caller (same account: identity policy is enough).
    runtimeRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ['lambda:InvokeFunctionUrl'],
        resources: [refreshFn.functionArn],
        conditions: { StringEquals: { 'lambda:FunctionUrlAuthType': 'AWS_IAM' } },
      }),
    );
    runtimeRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ['lambda:InvokeFunction'],
        resources: [refreshFn.functionArn],
        conditions: { Bool: { 'lambda:InvokedViaFunctionUrl': 'true' } },
      }),
    );

    // ---------------------------------------------------------------
    // Outputs for scripts/spikes/s2_scope_test.py
    // ---------------------------------------------------------------

    const out = (key: string, value: string, description: string) => new cdk.CfnOutput(this, key, { value, description });
    out('Region', this.region, 'Deploy Region');
    out('PolicyMode', mode, 'Gateway policy engine mode (LOG_ONLY or ENFORCE)');
    out('ResourceServerId', RESOURCE_SERVER_ID, 'Cognito resource server identifier; scope = <id>/<scope>');
    out('ScopeNames', Object.values(SCOPES).join(','), 'Scope names on the resource server');
    out('UserPoolId', userPool.userPoolId, 'Spike Cognito user pool');
    out('TokenEndpoint', tokenEndpoint, 'Cognito OAuth token endpoint');
    out('ClientId', m2mClient.userPoolClientId, 'M2M app client id');
    out('ClientSecretArn', clientSecret.secretArn, 'Secrets Manager secret holding the M2M client secret');
    out('RunnerRoleArn', runnerRole.roleArn, 'Only identity that can read the secret');
    out('RuntimeRoleArn', runtimeRole.roleArn, 'Must be denied the secret; calls the refresh stub');
    out('GatewayId', gateway.attrGatewayIdentifier, 'AgentCore Gateway id');
    out('GatewayArn', gateway.attrGatewayArn, 'AgentCore Gateway ARN');
    out('GatewayUrl', gateway.attrGatewayUrl, 'Gateway MCP endpoint');
    out('PolicyEngineId', policyEngine.attrPolicyEngineId, 'Policy engine id');
    out('PolicyEngineArn', policyEngine.attrPolicyEngineArn, 'Policy engine ARN');
    out('GatewayLogGroup', gatewayLogGroup.logGroupName, 'Gateway APPLICATION_LOGS (policy decisions)');
    out('TokenRefreshUrl', refreshUrl.url, 'IAM-authenticated token-refresh stub (B67)');
    out('TargetToolNames', ECHO_TARGETS.map((t) => `${t.targetName}___${TOOL_NAME}`).join(','), 'Tool names expected from tools/list');
    policies.forEach((p, i) => out(`PolicyId${i}`, p.attrPolicyId, 'Cedar policy id'));

    // ---------------------------------------------------------------
    // cdk-nag suppressions (ADR format)
    // ---------------------------------------------------------------

    NagSuppressions.addStackSuppressions(this, [
      {
        id: 'AwsSolutions-COG2',
        reason: 'ADR: No MFA on the spike user pool | Rationale: machine-to-machine client-credentials pool with no users | Alternative: MFA rejected — nothing to prompt',
      },
      {
        id: 'AwsSolutions-COG8',
        reason: 'ADR: Spike pool stays on the default feature plan | Rationale: throwaway pool with no human users; Plus-tier threat protection adds cost for no benefit | Alternative: FeaturePlan.PLUS rejected',
      },
      {
        id: 'AwsSolutions-SMG4',
        reason: 'ADR: No rotation on the M2M client secret | Rationale: the value is the Cognito app client secret, rotated by replacing the client, and the stack lives days | Alternative: a rotation Lambda rejected — no rotation API for a Cognito client secret here',
      },
      {
        id: 'AwsSolutions-IAM5',
        reason: 'ADR: Wildcards in spike roles | Rationale: runtime role GetSecretValue on * is intentional (proves the resource-policy Deny wins); gateway role uses gateway/* because the gateway ARN cannot be known before the role (circular); log-group write grants end in :* | Alternative: scoped ARNs rejected — circular or defeat the test',
      },
    ]);
  }
}
