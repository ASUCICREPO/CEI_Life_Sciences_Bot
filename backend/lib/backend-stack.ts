import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Aspects, Duration, RemovalPolicy } from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { AwsSolutionsChecks, NagSuppressions } from 'cdk-nag';
import { aws_bedrockagentcore as agentcore } from 'aws-cdk-lib';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as apigw from 'aws-cdk-lib/aws-apigateway';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as s3deploy from 'aws-cdk-lib/aws-s3-deployment';
import { DeployConfig, DEFAULT_LIMITS } from './config/deploy-config';
import { PythonDepsLayer } from './constructs/python-deps-layer';
// import * as sqs from 'aws-cdk-lib/aws-sqs';

export interface BackendStackProps extends cdk.StackProps {
  /** Parsed deploy config (`deploy.sh`-generated). Undefined for environment-agnostic `cdk synth`. */
  config?: DeployConfig;
  /** CLOUDFRONT-scope WAF Web ACL ARN from `EdgeWafStack` (always us-east-1, B88). Undefined when no config (no WAF stack yet). */
  webAclArn?: string;
  /** Directory holding the built SPA (`frontend/dist`), deployed to the site bucket. */
  frontendAssetPath: string;
}

export class BackendStack extends cdk.Stack {
  public readonly config?: DeployConfig;

  constructor(scope: Construct, id: string, props: BackendStackProps) {
    super(scope, id, props);

    this.config = props.config;

    // ADR: cdk-nag scoped to stack, not app entry point
    // Rationale: Self-contained security checks travel with the stack when used as a template
    // Alternative: Aspects.of(app) in bin/backend.ts (rejected - doesn't travel with stack)
    Aspects.of(this).add(new AwsSolutionsChecks({ verbose: true }));

    cdk.Tags.of(this).add('Project', 'CEI-Life-Sciences-Bot');

    // ---------------------------------------------------------------
    // Code Interpreter (AgentCore)
    // ---------------------------------------------------------------

    // ADR: Code Interpreter Public Network | Rationale: Life sciences agents require internet access to call external database APIs (NCBI, PubChem, etc.) and install Python packages | Alternative: Sandbox mode rejected — no internet blocks API access
    const codeInterpreter = new agentcore.CodeInterpreterCustom(this, 'CodeInterpreter', {
      codeInterpreterCustomName: 'ceiLifeSciencesCI',
      description: 'Code interpreter with internet access for life sciences agent tasks',
      networkConfiguration: agentcore.CodeInterpreterNetworkConfiguration.usingPublicNetwork(),
    });

    // When SessionRunnerLambda and the specialist AgentCore runtimes are added to this stack, call:
    //   codeInterpreter.grantUse(sessionRunnerLambda.role!);
    //   codeInterpreter.grantUse(agentRuntimeRole);
    // These grant: StartCodeInterpreterSession, InvokeCodeInterpreter, StopCodeInterpreterSession
    // Pass CEI_CODE_INTERPRETER_ID as an environment variable to those resources.

    new cdk.CfnOutput(this, 'CodeInterpreterId', {
      value: codeInterpreter.codeInterpreterId,
      exportName: 'CeiCodeInterpreterId',
      description: 'AgentCore Code Interpreter ID — reference as CEI_CODE_INTERPRETER_ID in agent runtimes',
    });

    if (this.config) {
      new cdk.CfnOutput(this, 'WebSearchEnabled', {
        value: String(this.config.webSearchEnabled),
        description: 'Whether the AgentCore Web Search connector is enabled for the deploy Region (B44)',
      });
    }

    // ---------------------------------------------------------------
    // Pre-sign-up domain allowlist trigger (B16)
    // ---------------------------------------------------------------

    const preSignUpLogGroup = new logs.LogGroup(this, 'PreSignUpDomainCheckLogs', {
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    // ADR: hand-rolled role, no AWSLambdaBasicExecutionRole | Rationale: avoids IAM4 (managed policy); grantWrite scopes log permissions to this exact log group only, avoiding an IAM5 wildcard resource
    const preSignUpRole = new iam.Role(this, 'PreSignUpDomainCheckRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
    });
    preSignUpLogGroup.grantWrite(preSignUpRole);

    const preSignUpFn = new lambda.Function(this, 'PreSignUpDomainCheckFn', {
      // ADR: Python 3.14 | Rationale: cdk-nag's Lambda-latest-runtime rule (L1) compares against the newest runtime this aws-cdk-lib release knows about per family
      runtime: lambda.Runtime.PYTHON_3_14,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', 'lambdas', 'pre-signup-domain-check'), {
        exclude: ['tests', '__pycache__', '*.pyc'],
      }),
      role: preSignUpRole,
      logGroup: preSignUpLogGroup,
      timeout: Duration.seconds(10),
      environment: {
        // ADR: empty string (not omitted) when there is no config | Rationale: an empty allowlist makes the trigger reject every sign-up, which is the safe default before deploy.sh has run
        ALLOWED_EMAIL_DOMAINS: (this.config?.allowedEmailDomains ?? []).join(','),
      },
    });

    // ---------------------------------------------------------------
    // Cognito User Pool + SPA client (B89: managed login, PKCE)
    // ---------------------------------------------------------------

    const userPool = new cognito.UserPool(this, 'UserPool', {
      selfSignUpEnabled: true,
      signInAliases: { email: true },
      autoVerify: { email: true },
      standardAttributes: {
        // ADR: email immutable | Rationale: security review — email is the sign-in identifier; allowing it to be changed post-verification would let a user pivot to an unverified/disallowed domain
        email: { required: true, mutable: false },
      },
      accountRecovery: cognito.AccountRecovery.EMAIL_ONLY,
      mfa: cognito.Mfa.OFF,
      passwordPolicy: {
        minLength: 12,
        requireLowercase: true,
        requireUppercase: true,
        requireDigits: true,
        requireSymbols: true,
      },
      lambdaTriggers: {
        preSignUp: preSignUpFn,
      },
      deletionProtection: true,
      // ADR: RETAIN | Rationale: user accounts are user data — a stack teardown must not silently delete every sign-up
      removalPolicy: RemovalPolicy.RETAIN,
    });

    // ---------------------------------------------------------------
    // Private site bucket + access-logs bucket + CloudFront distribution
    // ---------------------------------------------------------------

    const logsBucket = new s3.Bucket(this, 'AccessLogsBucket', {
      // ADR: OBJECT_WRITER ownership | Rationale: classic CloudFront/S3 access-log delivery still grants access via ACL; BUCKET_OWNER_ENFORCED (the S3 default) blocks ACLs entirely
      objectOwnership: s3.ObjectOwnership.OBJECT_WRITER,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: RemovalPolicy.RETAIN,
      // ADR: expire access logs after 90 days | Rationale: security review — unbounded log retention has no approved retention policy yet; 90 days is a reasonable default until one is set
      lifecycleRules: [{ enabled: true, expiration: Duration.days(90) }],
    });

    const siteBucket = new s3.Bucket(this, 'SiteBucket', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      serverAccessLogsBucket: logsBucket,
      serverAccessLogsPrefix: 's3-access-logs/',
      removalPolicy: RemovalPolicy.RETAIN,
    });

    // ---------------------------------------------------------------
    // REST API (B2): one Regional REST API; routes, authorizer and Lambdas are added further down.
    // Created before the CSP because connect-src needs its origin (RestApi id only, no cycle with the site).
    // ---------------------------------------------------------------

    const requestsPerSecond = (this.config?.limits ?? DEFAULT_LIMITS).requestsPerSecond;

    const apiAccessLogGroup = new logs.LogGroup(this, 'ApiAccessLogs', {
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    const api = new apigw.RestApi(this, 'Api', {
      restApiName: 'cei-life-sciences-api',
      endpointConfiguration: { types: [apigw.EndpointType.REGIONAL] },
      // ADR: cloudWatchRole on | Rationale: API access logging needs the account-level API Gateway CloudWatch role; CDK's context flag otherwise disables it
      cloudWatchRole: true,
      deployOptions: {
        stageName: 'v1',
        // ADR: ERROR-level execution logging without request/response bodies | Rationale: satisfies APIG6 and aids debugging without putting user chat data in logs (B7)
        loggingLevel: apigw.MethodLoggingLevel.ERROR,
        dataTraceEnabled: false,
        accessLogDestination: new apigw.LogGroupLogDestination(apiAccessLogGroup),
        accessLogFormat: apigw.AccessLogFormat.jsonWithStandardFields({
          caller: false,
          httpMethod: true,
          ip: true,
          protocol: true,
          requestTime: true,
          resourcePath: true,
          responseLength: true,
          status: true,
          user: false,
        }),
        // ADR: stage-wide default throttle in addition to the UsagePlan | Rationale: B22/B78 account-wide 20 rps; a UsagePlan only throttles requests that carry an API key, which Cognito-authenticated calls do not; no per-route limits
        throttlingRateLimit: requestsPerSecond,
        throttlingBurstLimit: requestsPerSecond,
      },
    });
    const apiOrigin = `https://${api.restApiId}.execute-api.${this.region}.${this.urlSuffix}`;

    // ADR: baseline CSP, computed without a real domain resource | Rationale: Cognito prefix-domain
    // hostnames are deterministic (prefix + ".auth." + region + ".amazoncognito.com"); the residual
    // Artifact-sandbox CSP interaction stays a TBD for F6/F21 (docs/cei-ui-protocol.md)
    const cognitoDomainPrefix = `cei-ls-${this.account}`;
    const cognitoDomainHost = `${cognitoDomainPrefix}.auth.${this.region}.amazoncognito.com`;
    const cognitoDomainUrl = `https://${cognitoDomainHost}`;
    const cognitoIdpHost = `cognito-idp.${this.region}.amazonaws.com`;
    const contentSecurityPolicy = [
      "default-src 'self'",
      "script-src 'self'",
      "style-src 'self' 'unsafe-inline'",
      "font-src 'self'",
      "img-src 'self' data:",
      `connect-src 'self' https://${cognitoIdpHost} ${cognitoDomainUrl} ${apiOrigin}`,
      "frame-ancestors 'none'",
      "object-src 'none'",
      "base-uri 'self'",
      `form-action 'self' ${cognitoDomainUrl}`,
    ].join('; ');

    const responseHeadersPolicy = new cloudfront.ResponseHeadersPolicy(this, 'SecurityHeadersPolicy', {
      securityHeadersBehavior: {
        contentSecurityPolicy: { contentSecurityPolicy, override: true },
        strictTransportSecurity: {
          accessControlMaxAge: Duration.days(365),
          includeSubdomains: true,
          preload: true,
          override: true,
        },
        contentTypeOptions: { override: true },
        frameOptions: { frameOption: cloudfront.HeadersFrameOption.DENY, override: true },
        referrerPolicy: {
          referrerPolicy: cloudfront.HeadersReferrerPolicy.STRICT_ORIGIN_WHEN_CROSS_ORIGIN,
          override: true,
        },
      },
      // ADR: Permissions-Policy + Cross-Origin-Opener-Policy | Rationale: security review — the dashboard
      // needs none of camera/microphone/geolocation/payment, and COOP: same-origin isolates the browsing
      // context group without breaking the Cognito hosted-UI top-level redirect flow (unlike COEP)
      customHeadersBehavior: {
        customHeaders: [
          {
            header: 'Permissions-Policy',
            value: 'camera=(), microphone=(), geolocation=(), payment=()',
            override: true,
          },
          { header: 'Cross-Origin-Opener-Policy', value: 'same-origin', override: true },
        ],
      },
    });

    const distribution = new cloudfront.Distribution(this, 'Distribution', {
      defaultRootObject: 'index.html',
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(siteBucket),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        responseHeadersPolicy,
      },
      errorResponses: [
        { httpStatus: 403, responseHttpStatus: 200, responsePagePath: '/index.html' },
        { httpStatus: 404, responseHttpStatus: 200, responsePagePath: '/index.html' },
      ],
      enableLogging: true,
      logBucket: logsBucket,
      logFilePrefix: 'cloudfront-access-logs/',
      webAclId: props.webAclArn,
    });

    // ---------------------------------------------------------------
    // SPA app client (public, PKCE, no secret) + managed-login domain/branding
    // ---------------------------------------------------------------

    const siteUrl = `https://${distribution.distributionDomainName}`;
    const callbackUrls = [`${siteUrl}/auth/callback`];
    const logoutUrls = [`${siteUrl}/`];
    if (this.config?.allowLocalhostCallback) {
      // ADR: localhost callback gated behind config.allowLocalhostCallback (B90) | Rationale: local dev proposal, not a settled CORS/dev-origin decision (security.md TBD) — never added unconditionally
      callbackUrls.push('http://localhost:5173/auth/callback');
      logoutUrls.push('http://localhost:5173/');
    }

    const spaClient = userPool.addClient('SpaClient', {
      generateSecret: false,
      oAuth: {
        flows: { authorizationCodeGrant: true },
        scopes: [cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL, cognito.OAuthScope.PROFILE],
        callbackUrls,
        logoutUrls,
      },
      preventUserExistenceErrors: true,
      accessTokenValidity: Duration.minutes(60),
      idTokenValidity: Duration.minutes(60),
      refreshTokenValidity: Duration.days(7),
      supportedIdentityProviders: [cognito.UserPoolClientIdentityProvider.COGNITO],
      // ADR: no direct-auth flows | Rationale: security review — this is a hosted-UI/PKCE-only client; SRP/custom/password/admin flows must stay off so credentials can only move through the managed-login OAuth endpoint
      // Alternative: `authFlows: {}` rejected — CDK's UserPoolClient omits ExplicitAuthFlows entirely when `authFlows` is undefined or empty (see configureAuthFlows in user-pool-client.js), which leaves Cognito's own default (SRP + custom auth enabled) in place instead of locking it down
      readAttributes: new cognito.ClientAttributes().withStandardAttributes({ email: true }),
      // ADR: empty writeAttributes | Rationale: security review — email is immutable at the schema level and there are no other user-editable profile fields yet, so no attribute needs app-client write access
      writeAttributes: new cognito.ClientAttributes(),
    });

    // ADR: escape hatch for ExplicitAuthFlows | Rationale: see the ADR above — this is the only way to force
    // CloudFormation to emit ExplicitAuthFlows: ['ALLOW_REFRESH_TOKEN_AUTH'] and nothing else
    const cfnSpaClient = spaClient.node.defaultChild as cognito.CfnUserPoolClient;
    cfnSpaClient.explicitAuthFlows = ['ALLOW_REFRESH_TOKEN_AUTH'];

    const userPoolDomain = userPool.addDomain('UserPoolDomain', {
      cognitoDomain: { domainPrefix: cognitoDomainPrefix },
      managedLoginVersion: cognito.ManagedLoginVersion.NEWER_MANAGED_LOGIN,
    });

    const managedLoginBranding = new cognito.CfnManagedLoginBranding(this, 'ManagedLoginBranding', {
      userPoolId: userPool.userPoolId,
      clientId: spaClient.userPoolClientId,
      useCognitoProvidedValues: true,
    });
    managedLoginBranding.node.addDependency(userPoolDomain);

    // ---------------------------------------------------------------
    // Session stores (B12, B52, B58): DynamoDB, AWS-managed encryption, PITR, no TTL, no GSI
    // ---------------------------------------------------------------

    const chatMetadataTable = new dynamodb.Table(this, 'ChatMetadataStore', {
      partitionKey: { name: 'user_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'session_id', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      deletionProtection: true,
      // ADR: RETAIN | Rationale: chat metadata is user data — a stack teardown must not silently delete it (B58: deleted only by the user)
      removalPolicy: RemovalPolicy.RETAIN,
    });

    const chatHistoryTable = new dynamodb.Table(this, 'ChatHistoryStore', {
      partitionKey: { name: 'session_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'sequence_number', type: dynamodb.AttributeType.NUMBER },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      deletionProtection: true,
      // ADR: RETAIN | Rationale: chat history is user data — a stack teardown must not silently delete it (B58: deleted only by the user)
      removalPolicy: RemovalPolicy.RETAIN,
    });

    // ---------------------------------------------------------------
    // Session Lambdas (Python 3.14) sharing one dependency layer
    // ---------------------------------------------------------------

    const allowedOrigins = [siteUrl];
    if (this.config?.allowLocalhostCallback) {
      // ADR: localhost origin gated behind config.allowLocalhostCallback (B90) | Rationale: same local-dev switch as the Cognito callback URLs; never added unconditionally
      allowedOrigins.push('http://localhost:5173');
    }

    const pythonDeps = new PythonDepsLayer(this, 'PythonDepsLayer');

    const sessionLambdaEnv = {
      CEI_AWS_REGION: this.region,
      CEI_CHAT_METADATA_TABLE: chatMetadataTable.tableName,
      CEI_CHAT_HISTORY_TABLE: chatHistoryTable.tableName,
      ALLOWED_ORIGINS: allowedOrigins.join(','),
    };

    const createSessionFunction = (id: string, dir: string, handlerName: string) => {
      const logGroup = new logs.LogGroup(this, `${id}Logs`, {
        retention: logs.RetentionDays.ONE_MONTH,
        removalPolicy: RemovalPolicy.DESTROY,
      });
      // ADR: hand-rolled role, no AWSLambdaBasicExecutionRole | Rationale: same as the pre-sign-up Lambda — avoids IAM4; grantWrite scopes logs to this exact log group
      const role = new iam.Role(this, `${id}Role`, { assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com') });
      logGroup.grantWrite(role);
      const fn = new lambda.Function(this, `${id}Fn`, {
        runtime: lambda.Runtime.PYTHON_3_14,
        handler: handlerName,
        code: lambda.Code.fromAsset(path.join(__dirname, '..', 'lambdas', dir), {
          exclude: ['tests', '__pycache__', '*.pyc'],
        }),
        layers: [pythonDeps.layer],
        role,
        logGroup,
        timeout: Duration.seconds(10),
        memorySize: 256,
        environment: sessionLambdaEnv,
      });
      return { fn, role };
    };

    const sessionMetadata = createSessionFunction(
      'SessionMetadataHandler',
      'session-metadata-handler',
      'metadata_handler.handler',
    );
    const sessionHistory = createSessionFunction(
      'SessionHistoryHandler',
      'session-history-handler',
      'history_handler.handler',
    );

    // Least privilege: exactly the actions each handler calls (no DeleteItem until F19, no Scan, no GSI wildcard)
    chatMetadataTable.grant(
      sessionMetadata.role,
      'dynamodb:PutItem',
      'dynamodb:GetItem',
      'dynamodb:Query',
      'dynamodb:UpdateItem',
    );
    // B12: history is keyed by session_id only, so the history handler proves ownership with a metadata GetItem first
    chatMetadataTable.grant(sessionHistory.role, 'dynamodb:GetItem');
    chatHistoryTable.grant(sessionHistory.role, 'dynamodb:Query');

    // ---------------------------------------------------------------
    // API routes: Cognito authorizer on every method, error-envelope gateway responses (api.md)
    // ---------------------------------------------------------------

    const authorizer = new apigw.CognitoUserPoolsAuthorizer(this, 'ApiAuthorizer', {
      cognitoUserPools: [userPool],
    });
    const requestValidator = api.addRequestValidator('ApiRequestValidator', {
      validateRequestParameters: true,
      validateRequestBody: true,
    });
    const methodOptions: apigw.MethodOptions = {
      authorizer,
      authorizationType: apigw.AuthorizationType.COGNITO,
      // ADR: openid scope | Rationale: the SPA sends the Cognito access token (not the ID token); an authorizer validates access tokens only when the method lists scopes
      authorizationScopes: ['openid'],
      requestValidator,
    };
    const corsOptions: apigw.CorsOptions = {
      allowOrigins: allowedOrigins,
      allowMethods: ['GET', 'POST', 'PATCH', 'OPTIONS'],
      allowHeaders: ['Authorization', 'Content-Type'],
      maxAge: Duration.hours(1),
    };

    const metadataIntegration = new apigw.LambdaIntegration(sessionMetadata.fn);
    const historyIntegration = new apigw.LambdaIntegration(sessionHistory.fn);

    const sessionsResource = api.root.addResource('sessions', { defaultCorsPreflightOptions: corsOptions });
    sessionsResource.addMethod('POST', metadataIntegration, methodOptions);
    sessionsResource.addMethod('GET', metadataIntegration, methodOptions);
    const sessionResource = sessionsResource.addResource('{sessionId}', { defaultCorsPreflightOptions: corsOptions });
    sessionResource.addMethod('GET', metadataIntegration, methodOptions);
    sessionResource.addMethod('PATCH', metadataIntegration, methodOptions);
    const historyResource = sessionResource.addResource('history', { defaultCorsPreflightOptions: corsOptions });
    historyResource.addMethod('GET', historyIntegration, methodOptions);

    // Gateway-generated errors (no Lambda ran) use the same envelope as the Lambdas. The static CORS origin is the
    // site; a localhost dev origin therefore sees these particular errors as CORS failures.
    const envelope = (code: string, message: string, retriable: boolean) =>
      `{"error":{"code":"${code}","message":"${message}","request_id":"$context.requestId","retriable":${retriable}}}`;
    const gatewayResponses: Array<[string, apigw.ResponseType, string, string, string, boolean]> = [
      ['Unauthorized', apigw.ResponseType.UNAUTHORIZED, '401', 'unauthorized', 'Authentication is required.', false],
      ['AccessDenied', apigw.ResponseType.ACCESS_DENIED, '403', 'forbidden', 'Access is denied.', false],
      ['Throttled', apigw.ResponseType.THROTTLED, '429', 'rate_limited', 'Too many requests. Try again shortly.', true],
      ['Default4xx', apigw.ResponseType.DEFAULT_4XX, '', 'invalid_request', 'The request could not be processed.', false],
      ['Default5xx', apigw.ResponseType.DEFAULT_5XX, '', 'internal_error', 'An internal error occurred.', false],
    ];
    for (const [name, type, statusCode, code, message, retriable] of gatewayResponses) {
      api.addGatewayResponse(`${name}Response`, {
        type,
        ...(statusCode ? { statusCode } : {}),
        responseHeaders: { 'Access-Control-Allow-Origin': `'${siteUrl}'` },
        templates: { 'application/json': envelope(code, message, retriable) },
      });
    }

    // ADR: UsagePlan carries the account-wide throttle (B22/B78) | Rationale: the decided control; the stage-wide default above is what actually enforces it for Cognito callers
    const usagePlan = api.addUsagePlan('ApiUsagePlan', {
      name: 'cei-account-wide',
      throttle: { rateLimit: requestsPerSecond, burstLimit: requestsPerSecond },
    });
    usagePlan.addApiStage({ stage: api.deploymentStage });

    // CORS preflight cannot carry credentials, so OPTIONS methods are the only unauthenticated ones
    for (const child of api.node.findAll()) {
      if (child instanceof apigw.CfnMethod && child.httpMethod === 'OPTIONS') {
        child.requestValidatorId = requestValidator.requestValidatorId;
        NagSuppressions.addResourceSuppressions(child, [
          { id: 'AwsSolutions-APIG4', reason: 'ADR: CORS preflight OPTIONS without authorizer | Rationale: browsers never send credentials on a preflight request; the method is a MOCK integration returning only CORS headers | Alternative: authorizer on OPTIONS rejected — would make every cross-origin call fail' },
          { id: 'AwsSolutions-COG4', reason: 'ADR: CORS preflight OPTIONS without Cognito authorizer | Rationale: browsers never send credentials on a preflight request; the method is a MOCK integration returning only CORS headers | Alternative: authorizer on OPTIONS rejected — would make every cross-origin call fail' },
        ]);
      }
    }

    NagSuppressions.addResourceSuppressions(
      api.node.findChild('CloudWatchRole'),
      [
        {
          id: 'AwsSolutions-IAM4',
          reason: 'ADR: AmazonAPIGatewayPushToCloudWatchLogs managed policy on the API Gateway account logging role | Rationale: CDK-owned role for the account-level CloudWatch setting that API access logs require; AWS documents this policy for exactly this purpose | Alternative: hand-written logs policy rejected because the role can only be assumed by API Gateway and is not under per-API control',
          appliesTo: ['Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AmazonAPIGatewayPushToCloudWatchLogs'],
        },
      ],
    );

    // ---------------------------------------------------------------
    // BucketDeployment — SPA assets, then config.json (no-cache) in a second
    // deployment so its Cache-Control never applies to the hashed SPA assets
    // ---------------------------------------------------------------

    const siteDeployment = new s3deploy.BucketDeployment(this, 'SiteAssetsDeployment', {
      sources: [s3deploy.Source.asset(props.frontendAssetPath)],
      destinationBucket: siteBucket,
      // ADR: exclude config.json | Rationale: this deployment prunes, and config.json is not in frontend/dist — a frontend-only change re-runs only this deployment, which deleted config.json while RuntimeConfigDeployment (unchanged) did not re-run; excluded keys are neither uploaded nor pruned | Alternative: prune: false rejected — stale hashed assets would pile up in the bucket
      exclude: ['config.json'],
      distribution,
      distributionPaths: ['/*'],
    });

    const apiBaseUrl = `${apiOrigin}/${api.deploymentStage.stageName}`;

    const configDeployment = new s3deploy.BucketDeployment(this, 'RuntimeConfigDeployment', {
      sources: [
        s3deploy.Source.jsonData('config.json', {
          region: this.region,
          userPoolId: userPool.userPoolId,
          userPoolClientId: spaClient.userPoolClientId,
          cognitoDomain: cognitoDomainUrl,
          apiBaseUrl,
        }),
      ],
      destinationBucket: siteBucket,
      // ADR: prune: false | Rationale: this deployment only ships config.json — pruning would delete every SPA asset the previous BucketDeployment just wrote
      prune: false,
      cacheControl: [s3deploy.CacheControl.noCache()],
      // ADR: explicit contentType | Rationale: config.json must be served as JSON; this also changed this deployment's properties so it re-ran once and restored the config.json that SiteAssetsDeployment had pruned
      contentType: 'application/json',
      distribution,
      distributionPaths: ['/config.json'],
    });
    configDeployment.node.addDependency(siteDeployment);

    // ---------------------------------------------------------------
    // Outputs (F3 needs the pool ID/ARN and client ID for the API authorizer)
    // ---------------------------------------------------------------

    new cdk.CfnOutput(this, 'UserPoolId', { value: userPool.userPoolId });
    new cdk.CfnOutput(this, 'UserPoolArn', { value: userPool.userPoolArn });
    new cdk.CfnOutput(this, 'UserPoolClientId', { value: spaClient.userPoolClientId });
    new cdk.CfnOutput(this, 'CognitoDomain', { value: cognitoDomainUrl });
    new cdk.CfnOutput(this, 'DistributionDomainName', { value: distribution.distributionDomainName });
    new cdk.CfnOutput(this, 'SiteUrl', { value: siteUrl });
    new cdk.CfnOutput(this, 'ApiBaseUrl', { value: apiBaseUrl });

    // ---------------------------------------------------------------
    // cdk-nag suppressions (ADR format) — see F2 phase file / plan for the approved list
    // ---------------------------------------------------------------

    NagSuppressions.addStackSuppressions(this, [
      {
        id: 'AwsSolutions-COG2',
        reason: 'ADR: No MFA on the Cognito user pool | Rationale: B14 — MFA is out of scope for this phase; self-sign-up dashboard users authenticate with email + strong password only | Alternative: Optional/Required MFA rejected — adds SMS cost and sign-up friction not yet approved',
      },
      {
        id: 'AwsSolutions-COG8',
        reason: "ADR: Cognito user pool stays on the Essentials feature plan, not Plus | Rationale: B88 defaults — Plus-tier threat protection (advanced security) has a per-MAU cost not yet approved | Alternative: FeaturePlan.PLUS rejected pending a cost decision",
      },
      {
        id: 'AwsSolutions-CFR1',
        reason: 'ADR: No CloudFront geo restriction | Rationale: B88 — the user base is not yet geographically scoped; a global-access dashboard needs no allow/deny list at this phase | Alternative: an allowlist was rejected — no approved country list exists yet',
      },
      {
        id: 'AwsSolutions-CFR4',
        reason: 'ADR: Default *.cloudfront.net certificate, no custom domain | Rationale: B88 — no custom domain/ACM certificate has been provisioned for this phase; the default CloudFront certificate is TLSv1-floor only because CloudFront enforces that for the shared cert | Alternative: a custom domain + ACM cert rejected — no domain has been registered yet',
      },
    ]);

    if (!props.webAclArn) {
      // ADR: CFR2 suppressed only when no WAF is attached | Rationale: B88 — the CLOUDFRONT-scope Web ACL
      // lives in EdgeWafStack and is only created once deploy.sh supplies a config; a real deploy always
      // attaches it via `webAclArn`, so this suppression must not apply once one is actually supplied
      // Alternative: a stack-wide unconditional suppression rejected — would silently mask a real deploy missing its WAF
      NagSuppressions.addResourceSuppressions(distribution, [
        {
          id: 'AwsSolutions-CFR2',
          reason: 'ADR: WAF is optional only for the environment-agnostic `cdk synth` (no deploy config yet) | Rationale: B88 — the CLOUDFRONT-scope Web ACL lives in EdgeWafStack and is only created once deploy.sh supplies a config; every real deploy attaches it via `webAclArn` | Alternative: hardcoding a WAF resource in this stack rejected — CLOUDFRONT-scope ACLs must live in us-east-1, forcing the two-stack split (B88)',
        },
      ]);
    }

    // ADR: L1/IAM4/IAM5 on the CDK-owned BucketDeployment custom-resource handler | Rationale: aws-cdk-lib's
    // BucketDeployment construct provisions its Lambda handler (and an AWSLambdaBasicExecutionRole-based role)
    // as a *singleton* directly under the Stack (aws-lambda's SingletonFunction always parents itself on
    // `Stack.of(scope)`, keyed by a config hash, deduplicating identical BucketDeployments) — it is not nested
    // under either `siteDeployment` or `configDeployment`, so the suppression has to target the stack child
    // directly | Alternative: a hand-rolled S3 upload custom resource was rejected — reimplements a
    // well-tested CDK-maintained construct for no security benefit
    const bucketDeploymentHandler = this.node.children.find((child) =>
      child.node.id.startsWith('Custom::CDKBucketDeployment'),
    );
    if (bucketDeploymentHandler) {
      NagSuppressions.addResourceSuppressions(
        bucketDeploymentHandler,
        [
          { id: 'AwsSolutions-L1', reason: "ADR: CDK-owned BucketDeployment handler runtime | Rationale: pinned by aws-cdk-lib, not this stack's code | Alternative: none — not under this stack's control" },
          { id: 'AwsSolutions-IAM4', reason: "ADR: CDK-owned BucketDeployment handler role | Rationale: aws-cdk-lib attaches AWSLambdaBasicExecutionRole internally, not this stack's code | Alternative: none — not under this stack's control" },
          { id: 'AwsSolutions-IAM5', reason: "ADR: CDK-owned BucketDeployment handler role | Rationale: aws-cdk-lib grants itself wildcard S3/CloudFront invalidation permissions internally, not this stack's code | Alternative: none — not under this stack's control" },
        ],
        true,
      );
    }

    // ---------------------------------------------------------------
    // cdk-nag suppression examples — uncomment and adapt when needed
    // Common findings:
    //   AwsSolutions-IAM4 : AWS managed policy used (e.g. AWSLambdaBasicExecutionRole)
    //   AwsSolutions-IAM5 : Wildcard permissions — scope to specific resource ARNs
    //   AwsSolutions-S1   : S3 access logging disabled — enable for sensitive buckets
    //   AwsSolutions-L1   : Lambda runtime not latest — update to python3.12+
    //
    // Stack-level suppression (applies to all resources in this stack):
    // NagSuppressions.addStackSuppressions(this, [
    //   { id: 'AwsSolutions-IAM4', reason: 'ADR: <title> | Rationale: <why> | Alternative: <rejected>' },
    // ]);
    //
    // Resource-level suppression (applies to one specific construct):
    // NagSuppressions.addResourceSuppressions(myResource, [
    //   { id: 'AwsSolutions-IAM4', reason: 'ADR: <title> | Rationale: <why> | Alternative: <rejected>' },
    // ]);
    // ---------------------------------------------------------------
  }
}
