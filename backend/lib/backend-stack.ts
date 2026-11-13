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
import * as s3n from 'aws-cdk-lib/aws-s3-notifications';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import { SqsEventSource } from 'aws-cdk-lib/aws-lambda-event-sources';
import { DeployConfig, DEFAULT_LIMITS, DEFAULT_TOOL_POLICY_MODE } from './config/deploy-config';
import { PythonDepsLayer } from './constructs/python-deps-layer';
import { AgentRuntimes } from './constructs/agent-runtimes';
import { ModelGuardrail } from './constructs/model-guardrail';
import { ModelSettings } from './constructs/model-settings';
import { ToolGateway, fullScope, loadToolSchemas, RESERVED_SOURCE_IDS } from './constructs/tool-gateway';
import { SessionRouter } from './constructs/session-router';
import { suppressXrayWildcard } from './constructs/tracing';
import { Observability } from './constructs/observability';
import { BackgroundJobs } from './constructs/background-jobs';
import { AccountDeletion } from './constructs/account-deletion';
import { AttachmentsKnowledgeBase } from './constructs/attachments-knowledge-base';

// B109/F19: session-runner and ui-actions both require('../shared/account-gate') (backend/lambdas/shared/
// account-gate.js). A plain `Code.fromAsset(lambdas/<dir>)` zips only that one directory, so `shared/` would
// be missing at the Lambda's runtime filesystem; this stages the zip root at `lambdas/` instead, with every
// sibling excluded except the one Lambda's own directory and this one shared file -- the exact same relative
// layout (`<dir>/index.js` next to `shared/account-gate.js`) that already works for local `require` and tests.
function nodeLambdaCode(dir: string): lambda.Code {
  return lambda.Code.fromAsset(path.join(__dirname, '..', 'lambdas'), {
    exclude: ['*', '!' + dir, '!' + dir + '/**', '!shared', '!shared/account-gate.js', dir + '/*.test.js'],
  });
}

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
  /** The exact object shipped as the SPA's /config.json (set once, below, before RuntimeConfigDeployment). */
  public readonly runtimeConfig!: Record<string, unknown>;

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

    // The specialist runtimes get grantUse + CEI_CODE_INTERPRETER_ID in AgentRuntimes (below). When
    // SessionRunnerLambda is added, call codeInterpreter.grantUse(sessionRunnerLambda.role!) as well.

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
      tracing: lambda.Tracing.ACTIVE,
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
    suppressXrayWildcard(preSignUpRole);

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

    // ADR: AttachmentsBucket lives in BackendStack next to AgentRuntimes | Rationale: same-stack Ref/GetAtt only — no
    // cross-stack export, so no stack cycle and no export lock on the name; CloudFormation generates the name and the
    // runtimes receive it as CEI_ATTACHMENTS_BUCKET. No event notifications here (bucket -> Lambda -> bucket would cycle)
    const attachmentsBucket = new s3.Bucket(this, 'AttachmentsBucket', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      serverAccessLogsBucket: logsBucket,
      serverAccessLogsPrefix: 'attachments-access-logs/',
      // ADR: RETAIN | Rationale: attachments are user data — a stack teardown must not delete them
      removalPolicy: RemovalPolicy.RETAIN,
      // B32: the SPA uploads with a presigned POST whose policy holds the 55 MB `content-length-range`.
      // ADR: CORS origin '*' for POST only | Rationale: the signed POST policy (key, content type, size,
      // 5-minute expiry) is the access control, not the origin; naming the CloudFront origin here would make a
      // cycle (CSP response headers policy -> this bucket -> distribution) | Alternative: a custom resource
      // that sets CORS after the distribution exists was rejected (more resources under the 500 limit)
      cors: [{
        allowedMethods: [s3.HttpMethods.POST],
        allowedOrigins: ['*'],
        allowedHeaders: ['*'],
        maxAge: 3000,
      }],
    });

    // Export objects are written directly with the Intelligent-Tiering storage class.
    const exportsBucket = new s3.Bucket(this, 'ExportsBucket', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      serverAccessLogsBucket: logsBucket,
      serverAccessLogsPrefix: 'exports-access-logs/',
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
        tracingEnabled: true,
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
    // ADR: region-scoped Lambda function URL wildcard for the agent proxy | Rationale: the SPA calls the agents
    // through the AgentProxy function URL (config.json agentUrls); its exact host is a deploy-time attribute of
    // AWS::Lambda::Url, and that resource's CORS origin is this distribution, so naming the exact host here would
    // create a Distribution -> ResponseHeadersPolicy -> Url -> Distribution cycle | Alternative: exact host rejected (cycle)
    const agentProxyOriginPattern = `https://*.lambda-url.${this.region}.on.aws`;
    // ADR: S3 bucket in CSP for presigned uploads | Rationale: the SPA uploads attachments directly to S3
    // using presigned URLs; the bucket domain must be in connect-src to allow fetch/XHR | Alternative: proxy
    // uploads through API Gateway rejected — larger payloads, higher latency, no streaming, Lambda timeout risk
    const attachmentsBucketOrigin = `https://${attachmentsBucket.bucketDomainName}`;
    const contentSecurityPolicy = [
      "default-src 'self'",
      "script-src 'self'",
      "style-src 'self' 'unsafe-inline'",
      "font-src 'self'",
      "img-src 'self' data: https:",
      `connect-src 'self' https://${cognitoIdpHost} ${cognitoDomainUrl} ${apiOrigin} ${agentProxyOriginPattern} ${attachmentsBucketOrigin}`,
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

    // SPA fallback belongs to the static origin only: distribution-wide 403/404 rewrites
    // would turn authenticated API errors into a 200 HTML document.
    const spaFallback = new cloudfront.Function(this, 'SpaFallback', {
      code: cloudfront.FunctionCode.fromInline("function handler(event) { var r=event.request; if (r.uri.indexOf('.') === -1) r.uri='/index.html'; return r; }"),
    });
    const distribution = new cloudfront.Distribution(this, 'Distribution', {
      defaultRootObject: 'index.html',
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(siteBucket),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        responseHeadersPolicy,
        functionAssociations: [{ function: spaFallback, eventType: cloudfront.FunctionEventType.VIEWER_REQUEST }],
      },
      additionalBehaviors: {
        'chat/stream': {
          origin: new origins.HttpOrigin(`${api.restApiId}.execute-api.${this.region}.${this.urlSuffix}`, { originPath: '/v1' }),
          allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
          cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
          originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
          compress: false,
          viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        },
      },
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
    // Session stores (B12, B52, B58): DynamoDB, AWS-managed encryption, PITR, no TTL (kept forever)
    // ChatMetadataStore carries sort-key GSIs (by_created / by_updated / by_activity) so the history list can be
    // ordered by any timestamp, plus by_title for title search; all stay partitioned by user_id so a query can
    // never cross users
    // ---------------------------------------------------------------

    // NOTE: DynamoDB allows only ONE new GSI per table update. Adding several GSIs to this existing table in one
    // deploy fails the stack update — add one per deploy, or create them out-of-band and re-import the table.
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

    // ADR: two GSIs keyed (user_id, created_at|updated_at), projection ALL | Rationale: the history list sorts
    // by either timestamp (user toggle) and ALL avoids a follow-up GetItem per row; timestamps are UTC ISO-8601
    // with a 'Z' suffix so lexicographic order == time order | Alternative: sorting in the Lambda rejected —
    // breaks pagination.
    chatMetadataTable.addGlobalSecondaryIndex({
      indexName: 'by_created',
      partitionKey: { name: 'user_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'created_at', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });
    chatMetadataTable.addGlobalSecondaryIndex({
      indexName: 'by_updated',
      partitionKey: { name: 'user_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'updated_at', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });
    // ADR: by_activity keyed (user_id, last_activity_at) | Rationale: the default history order is "last used by
    // the user" — last_activity_at moves only when the user sends a message, so an old chat that gets a new message
    // jumps above newer chats; updated_at also moves on renames / generated titles / AI answers | Alternative:
    // reusing by_updated rejected (a rename would reorder the list).
    chatMetadataTable.addGlobalSecondaryIndex({
      indexName: 'by_activity',
      partitionKey: { name: 'user_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'last_activity_at', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });
    // ADR: by_title keyed (user_id, title_search) | Rationale: title_search is the case-folded chat title, kept in
    // step with every title write; the search query reads only the caller's partition of this index (contains()
    // filter for substring matches, title order, cursor = last returned row) | Alternative: OpenSearch rejected for
    // the PoC (cost + another store) — revisit if users need relevance-ranked or message-body search.
    chatMetadataTable.addGlobalSecondaryIndex({
      indexName: 'by_title',
      partitionKey: { name: 'user_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'title_search', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });
    const CHAT_METADATA_INDEXES = ['by_created', 'by_updated', 'by_activity', 'by_title'];
    const chatMetadataIndexArns = CHAT_METADATA_INDEXES.map(
      (name) => `${chatMetadataTable.tableArn}/index/${name}`,
    );

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
    // Attachment metadata store + KB bucket (file upload/RAG)
    // ---------------------------------------------------------------

    const attachmentsTable = new dynamodb.Table(this, 'AttachmentsMetadataStore', {
      partitionKey: { name: 'user_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'attachment_id', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      deletionProtection: true,
      removalPolicy: RemovalPolicy.RETAIN,
    });
    // GSI to query attachments by session
    attachmentsTable.addGlobalSecondaryIndex({
      indexName: 'by_session',
      partitionKey: { name: 'user_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'session_id', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // B110 (supersedes B33/B77): Managed Knowledge Base for attachment retrieval -- no S3 Vectors/KB chunks
    // bucket. The processor ingests extracted text straight into the KB via IngestKnowledgeBaseDocuments.
    const attachmentsKb = new AttachmentsKnowledgeBase(this, 'AttachmentsKnowledgeBase', { attachmentsBucket });

    // ---------------------------------------------------------------
    // Session Lambdas (Python 3.14) sharing one dependency layer
    // ---------------------------------------------------------------

    const allowedOrigins = [siteUrl];
    if (this.config?.allowLocalhostCallback) {
      // ADR: localhost origin gated behind config.allowLocalhostCallback (B90) | Rationale: same local-dev switch as the Cognito callback URLs; never added unconditionally
      allowedOrigins.push('http://localhost:5173');
    }

    const pythonDeps = new PythonDepsLayer(this, 'PythonDepsLayer');

    const exportsTable = new dynamodb.Table(this, 'ExportMetadataStore', {
      partitionKey: { name: 'user_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'export_id', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: RemovalPolicy.RETAIN,
    });

    const sessionLambdaEnv = {
      CEI_AWS_REGION: this.region,
      CEI_CHAT_METADATA_TABLE: chatMetadataTable.tableName,
      CEI_CHAT_HISTORY_TABLE: chatHistoryTable.tableName,
      ALLOWED_ORIGINS: allowedOrigins.join(','),
      // F8/B18: session-metadata-handler's _validate_model_id (create/PATCH) needs the same blocklist as /models
      CEI_BLOCKED_MODELS: (this.config?.blockedModelIds ?? []).join(','),
    };

    const createSessionFunction = (id: string, dir: string, handlerName: string,
      environment: Record<string, string> = sessionLambdaEnv, layers: lambda.ILayerVersion[] = [pythonDeps.layer]) => {
      const logGroup = new logs.LogGroup(this, `${id}Logs`, {
        retention: logs.RetentionDays.ONE_MONTH,
        removalPolicy: RemovalPolicy.DESTROY,
      });
      // ADR: hand-rolled role, no AWSLambdaBasicExecutionRole | Rationale: same as the pre-sign-up Lambda — avoids IAM4; grantWrite scopes logs to this exact log group
      const role = new iam.Role(this, `${id}Role`, { assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com') });
      logGroup.grantWrite(role);
      const fn = new lambda.Function(this, `${id}Fn`, {
        tracing: lambda.Tracing.ACTIVE,
        runtime: lambda.Runtime.PYTHON_3_14,
        handler: handlerName,
        code: lambda.Code.fromAsset(path.join(__dirname, '..', 'lambdas', dir), {
          exclude: ['tests', '__pycache__', '*.pyc'],
        }),
        layers,
        role,
        logGroup,
        timeout: Duration.seconds(10),
        memorySize: 256,
        environment,
      });
      suppressXrayWildcard(role);
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
    const exportsHandler = createSessionFunction('ExportsHandler', 'exports-handler', 'handler.handler', {
      ...sessionLambdaEnv,
      CEI_EXPORT_METADATA_TABLE: exportsTable.tableName,
      CEI_EXPORT_BUCKET: exportsBucket.bucketName,
    });
    exportsTable.grant(exportsHandler.role, 'dynamodb:GetItem', 'dynamodb:Query');
    exportsHandler.role.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['s3:GetObject'], resources: [exportsBucket.arnForObjects('exports/*')],
    }));
    NagSuppressions.addResourceSuppressions(exportsHandler.role, [{
      id: 'AwsSolutions-IAM5',
      reason: 'ADR: Export object read scope | Rationale: report object keys are generated at runtime under one bucket prefix | Alternative: fixed object ARNs rejected because exports do not exist at deployment',
    }], true);
    exportsHandler.role.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['dynamodb:GetItem'], resources: [chatMetadataTable.tableArn],
    }));
    chatHistoryTable.grant(exportsHandler.role, 'dynamodb:Query');
    const clientDiagnostics = createSessionFunction('ClientDiagnostics', 'client-diagnostics', 'handler.handler',
      { ALLOWED_ORIGINS: allowedOrigins.join(',') }, []);

    // Least privilege: exactly the actions each handler calls (no DeleteItem until F19, no Scan, no GSI wildcard)
    // ADR: explicit PolicyStatements on the metadata table | Rationale: Table.grant() appends `<table>/index/*`
    // once a table has GSIs (cdk-nag IAM5); naming the two indexes keeps the grant exact
    sessionMetadata.role.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ['dynamodb:PutItem', 'dynamodb:GetItem', 'dynamodb:Query', 'dynamodb:UpdateItem'],
        resources: [chatMetadataTable.tableArn],
      }),
    );
    sessionMetadata.role.addToPrincipalPolicy(
      new iam.PolicyStatement({ actions: ['dynamodb:Query'], resources: chatMetadataIndexArns }),
    );
    // GET/PATCH /sessions/{id} prove ownership against the history partition's stamped owner (owner_of) first
    chatHistoryTable.grant(sessionMetadata.role, 'dynamodb:Query');
    // F8/B18: POST /sessions validates model_id against server discovery before saving it (fail closed,
    // MODEL_POLICY.md step 2) — same discovery actions as model-settings.ts, same "no resource ARN" rationale.
    sessionMetadata.role.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['bedrock:ListFoundationModels', 'bedrock:ListInferenceProfiles', 'bedrock:GetInferenceProfile'],
      resources: ['*'],
    }));
    NagSuppressions.addResourceSuppressions(sessionMetadata.role, [{
      id: 'AwsSolutions-IAM5',
      reason: 'ADR: Bedrock discovery actions have no resource-level ARN | Rationale: ListFoundationModels/ListInferenceProfiles/GetInferenceProfile are account+Region-wide read-only list/describe operations; AWS defines no resource type to scope them to | Alternative: per-model ARNs rejected — not applicable to list operations',
    }], true);
    // B12: history is keyed by session_id only, so the history handler proves ownership with a metadata GetItem first
    sessionHistory.role.addToPrincipalPolicy(
      new iam.PolicyStatement({ actions: ['dynamodb:GetItem'], resources: [chatMetadataTable.tableArn] }),
    );
    chatHistoryTable.grant(sessionHistory.role, 'dynamodb:Query');
    // F6 B59: GET .../turns/{messageId}/trace reads the trace head (GetItem) and its chunks (BatchGetItem)
    chatHistoryTable.grant(sessionHistory.role, 'dynamodb:GetItem', 'dynamodb:BatchGetItem');

    // ---------------------------------------------------------------
    // Attachment presign + processor Lambdas (file upload/RAG)
    // ---------------------------------------------------------------

    const attachmentPresign = createSessionFunction(
      'AttachmentPresign',
      'attachment-presign',
      'handler.handler',
      {
        ATTACHMENTS_BUCKET: attachmentsBucket.bucketName,
        ATTACHMENTS_TABLE: attachmentsTable.tableName,
        ALLOWED_ORIGINS: allowedOrigins.join(','),
      },
      [],
    );
    // Presign Lambda needs: PutObject presign on attachments bucket, PutItem on attachments table
    attachmentPresign.role.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ['s3:PutObject'],
        resources: [`${attachmentsBucket.bucketArn}/uploads/*`],
      }),
    );
    attachmentPresign.role.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ['dynamodb:PutItem'],
        resources: [attachmentsTable.tableArn],
      }),
    );
    NagSuppressions.addResourceSuppressions(
      attachmentPresign.role,
      [{
        id: 'AwsSolutions-IAM5',
        reason: 'ADR: Wildcard in S3 path for uploads prefix | Rationale: presign Lambda generates URLs for user-specific paths under uploads/; the wildcard is constrained to that prefix only | Alternative: per-user policies rejected — not practical for multi-tenant',
      }],
      true,
    );

    // Processor Lambda — triggered by S3 events via SQS (B110: S3 -> SQS(+DLQ) -> processor, not S3 -> Lambda
    // directly, so a transient IngestKnowledgeBaseDocuments failure retries instead of being dropped)
    const processorTimeout = Duration.minutes(5);
    const attachmentProcessorDlq = new sqs.Queue(this, 'AttachmentProcessorDLQ', {
      enforceSSL: true,
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      retentionPeriod: Duration.days(14),
    });
    NagSuppressions.addResourceSuppressions(attachmentProcessorDlq, [{
      id: 'AwsSolutions-SQS3',
      reason: 'ADR: DLQ has no DLQ of its own | Rationale: this queue is the end of the retry chain for failed attachment-processing messages; a further redrive target has nothing left to catch | Alternative: a second-level DLQ rejected -- no failure mode it would catch',
    }], true);
    const attachmentProcessorQueue = new sqs.Queue(this, 'AttachmentProcessorQueue', {
      enforceSSL: true,
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      // ADR: 6x the Lambda timeout | Rationale: SQS best practice -- the visibility timeout must comfortably
      // exceed the consumer's processing time so a slow IngestKnowledgeBaseDocuments call never causes a duplicate delivery
      visibilityTimeout: Duration.seconds(processorTimeout.toSeconds() * 6),
      deadLetterQueue: { queue: attachmentProcessorDlq, maxReceiveCount: 3 },
    });

    const processorLogGroup = new logs.LogGroup(this, 'AttachmentProcessorLogs', {
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: RemovalPolicy.DESTROY,
    });
    const processorRole = new iam.Role(this, 'AttachmentProcessorRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
    });
    processorLogGroup.grantWrite(processorRole);
    suppressXrayWildcard(processorRole);

    // Explicit permissions avoid IAM5 wildcards from .grant*() calls
    processorRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ['s3:GetObject'],
        resources: [`${attachmentsBucket.bucketArn}/uploads/*`],
      }),
    );
    processorRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ['dynamodb:GetItem', 'dynamodb:UpdateItem'],
        resources: [attachmentsTable.tableArn],
      }),
    );
    processorRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ['bedrock:IngestKnowledgeBaseDocuments', 'bedrock:GetKnowledgeBaseDocuments'],
        resources: [attachmentsKb.knowledgeBaseArn],
      }),
    );
    NagSuppressions.addResourceSuppressions(
      processorRole,
      [{
        id: 'AwsSolutions-IAM5',
        reason: 'ADR: Wildcard in S3 path for uploads prefix | Rationale: processor reads whichever object the SQS message names under uploads/; object keys are per-user/session/attachment and created at runtime | Alternative: per-file policies rejected — impractical',
      }],
      true,
    );

    const attachmentProcessor = new lambda.Function(this, 'AttachmentProcessorFn', {
      tracing: lambda.Tracing.ACTIVE,
      runtime: lambda.Runtime.PYTHON_3_14,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', 'lambdas', 'attachment-processor'), {
        exclude: ['tests', '__pycache__', '*.pyc'],
      }),
      role: processorRole,
      logGroup: processorLogGroup,
      timeout: processorTimeout,
      memorySize: 1024,
      environment: {
        ATTACHMENTS_TABLE: attachmentsTable.tableName,
        ATTACHMENTS_BUCKET: attachmentsBucket.bucketName,
        KNOWLEDGE_BASE_ID: attachmentsKb.knowledgeBaseId,
        KNOWLEDGE_BASE_DATA_SOURCE_ID: attachmentsKb.dataSourceId,
      },
      layers: [pythonDeps.layer],
    });
    attachmentProcessor.addEventSource(new SqsEventSource(attachmentProcessorQueue, {
      batchSize: 1,
      reportBatchItemFailures: true,
    }));

    // S3 notification enqueues onto SQS on uploads; the processor consumes via the event source above
    attachmentsBucket.addEventNotification(
      s3.EventType.OBJECT_CREATED,
      new s3n.SqsDestination(attachmentProcessorQueue),
      { prefix: 'uploads/' },
    );

    // ADR: suppress the CDK-owned BucketNotificationsHandler custom resource | Rationale: CDK creates this
    // singleton Lambda with AWSLambdaBasicExecutionRole to manage S3 event notifications; not under this stack's control
    const notificationHandler = this.node.children.find((child) =>
      child.node.id.startsWith('BucketNotificationsHandler'),
    );
    if (notificationHandler) {
      NagSuppressions.addResourceSuppressions(
        notificationHandler,
        [
          { id: 'AwsSolutions-IAM4', reason: "ADR: CDK-owned BucketNotificationsHandler role | Rationale: aws-cdk-lib attaches AWSLambdaBasicExecutionRole internally | Alternative: none — not under this stack's control", appliesTo: ['Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole'] },
        ],
        true,
      );
    }

    // ---------------------------------------------------------------
    // AgentCore runtimes (plan 3b4d4f64 decision 9): Cognito JWT inbound authorizer + Authorization header
    // forwarded + CEI_COGNITO_* settings so identity.py re-verifies the access token; no auth-bypass env var.
    // agentcore.json stays local-development-only (docs/architectureDeepDive.md).
    // ---------------------------------------------------------------

    // F8/B51: input-only PII Guardrail (B51a), applied via the native guardrailConfig on every get_model()
    // call (shared/utils.py) — not added to BackendStack until now, per model-guardrail.ts's own note.
    const modelGuardrail = new ModelGuardrail(this, 'ModelGuardrail');

    const agentRuntimes = new AgentRuntimes(this, 'AgentRuntimes', {
      defaultModelId: this.config?.defaultModelId,
      blockedModelIds: this.config?.blockedModelIds,
      guardrailId: modelGuardrail.guardrailId,
      guardrailVersion: modelGuardrail.guardrailVersion,
      guardrailArn: modelGuardrail.guardrailArn,
      userPool,
      spaClient,
      chatMetadataTable,
      chatMetadataIndexArns,
      chatHistoryTable,
      codeInterpreter,
      attachmentsBucket,
      knowledgeBaseId: attachmentsKb.knowledgeBaseId,
      knowledgeBaseArn: attachmentsKb.knowledgeBaseArn,
      // Same origin set as the sessions API CORS; the containers answer CORS only if a browser ever reaches them
      // directly (the SPA goes through the agent proxy below)
      allowedOrigins,
      // B44/B45: same value ToolGateway uses to decide whether to build the Web Search target (below)
      webSearchEnabled: this.config?.webSearchEnabled,
    });

    // live-drafting: Drafting persists its own exports (drafting/nodes.py export_documents ->
    // shared/persistence/exports.py ExportRepository), Drafting-only for now (see configureDraftingExports).
    agentRuntimes.configureDraftingExports(exportsTable, exportsBucket);

    // ---------------------------------------------------------------
    // Agent proxy: Lambda function URL in front of the AgentCore runtimes
    // ---------------------------------------------------------------

    // ADR: browser -> Lambda function URL -> AgentCore, not browser -> AgentCore | Rationale: AgentCore documents no
    // CORS support and a preflight never carries the Authorization header its JWT authorizer needs; function URL
    // CORS is documented and answered by the Lambda service. The proxy forwards the caller's own bearer token, so
    // AgentCore's Cognito authorizer + identity.py still authenticate every call | Alternatives: API Gateway
    // HTTP_PROXY+STREAM rejected (proxy responses cannot get CORS headers added); CloudFront behaviour rejected
    // (60 s origin response timeout vs. multi-minute agent runs, and the SPA 403/404 -> index.html rewrite)
    const agentProxyLogGroup = new logs.LogGroup(this, 'AgentProxyLogs', {
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: RemovalPolicy.DESTROY,
    });
    // ADR: hand-rolled role with logs only | Rationale: AgentCore authorizes the forwarded user token, so the proxy needs no AWS permission on the runtimes
    const agentProxyRole = new iam.Role(this, 'AgentProxyRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
    });
    agentProxyLogGroup.grantWrite(agentProxyRole);
    const agentProxyFn = new lambda.Function(this, 'AgentProxyFn', {
      tracing: lambda.Tracing.ACTIVE,
      runtime: lambda.Runtime.PYTHON_3_14,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', 'lambdas', 'agent-proxy'), {
        exclude: ['tests', '__pycache__', '*.pyc', '.pytest_cache'],
      }),
      role: agentProxyRole,
      logGroup: agentProxyLogGroup,
      // ADR: 15-minute timeout | Rationale: agent runs are synchronous and the SPA waits up to 10 minutes (RUN_TIMEOUT_MS)
      timeout: Duration.minutes(15),
      memorySize: 256,
      environment: {
        AGENT_URLS: this.toJsonString(agentRuntimes.invocationUrls),
        UPSTREAM_TIMEOUT_SECONDS: '870',
      },
    });
    suppressXrayWildcard(agentProxyRole);
    const agentProxyUrl = agentProxyFn.addFunctionUrl({
      // ADR: authType NONE | Rationale: the caller authenticates with a Cognito bearer token that AgentCore verifies;
      // AWS_IAM would need SigV4-signed browser requests | Alternative: AWS_IAM rejected (no IAM credentials in the SPA)
      authType: lambda.FunctionUrlAuthType.NONE,
      invokeMode: lambda.InvokeMode.BUFFERED,
      cors: {
        allowedOrigins,
        allowedMethods: [lambda.HttpMethod.POST],
        allowedHeaders: ['authorization', 'content-type', 'accept', 'x-amzn-bedrock-agentcore-runtime-session-id'],
        allowCredentials: false,
        maxAge: Duration.hours(1),
      },
    });
    const agentNames = ['research', 'drafting', 'analysis', 'pipeline'] as const;
    const agentProxyUrls = Object.fromEntries(
      agentNames.map((name) => [name, cdk.Fn.join('', [agentProxyUrl.url, name])]),
    ) as Record<(typeof agentNames)[number], string>;

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
      // PUT: F8's GET/PUT /settings (model-settings.ts)
      allowMethods: ['GET', 'POST', 'PATCH', 'PUT', 'OPTIONS'],
      allowHeaders: ['Authorization', 'Content-Type', 'Accept', 'X-Amz-Date', 'X-Amz-Security-Token'],
      maxAge: Duration.hours(1),
    };

    const metadataIntegration = new apigw.LambdaIntegration(sessionMetadata.fn);
    const historyIntegration = new apigw.LambdaIntegration(sessionHistory.fn);

    const diagnosticsResource = api.root.addResource('client-diagnostics', { defaultCorsPreflightOptions: corsOptions });
    diagnosticsResource.addMethod('POST', new apigw.LambdaIntegration(clientDiagnostics.fn), methodOptions);

    const sessionsResource = api.root.addResource('sessions', { defaultCorsPreflightOptions: corsOptions });
    sessionsResource.addMethod('POST', metadataIntegration, methodOptions);
    sessionsResource.addMethod('GET', metadataIntegration, methodOptions);
    const sessionResource = sessionsResource.addResource('{sessionId}', { defaultCorsPreflightOptions: corsOptions });
    sessionResource.addMethod('GET', metadataIntegration, methodOptions);
    sessionResource.addMethod('PATCH', metadataIntegration, methodOptions);
    const historyResource = sessionResource.addResource('history', { defaultCorsPreflightOptions: corsOptions });
    historyResource.addMethod('GET', historyIntegration, methodOptions);
    const sessionExports = sessionResource.addResource('exports', { defaultCorsPreflightOptions: corsOptions });
    sessionExports.addMethod('GET', new apigw.LambdaIntegration(exportsHandler.fn), methodOptions);
    const exportsResource = api.root.addResource('exports', { defaultCorsPreflightOptions: corsOptions });
    const exportResource = exportsResource.addResource('{exportId}', { defaultCorsPreflightOptions: corsOptions });
    exportResource.addResource('download', { defaultCorsPreflightOptions: corsOptions })
      .addMethod('GET', new apigw.LambdaIntegration(exportsHandler.fn), methodOptions);
    // F6 B59: lazy per-turn trace, fetched only when the user opens the thinking block
    const turnsResource = sessionResource.addResource('turns', { defaultCorsPreflightOptions: corsOptions });
    const turnResource = turnsResource.addResource('{messageId}', { defaultCorsPreflightOptions: corsOptions });
    const traceResource = turnResource.addResource('trace', { defaultCorsPreflightOptions: corsOptions });
    traceResource.addMethod('GET', historyIntegration, methodOptions);

    // Attachments presign route
    const attachmentsResource = api.root.addResource('attachments', { defaultCorsPreflightOptions: corsOptions });
    const presignResource = attachmentsResource.addResource('presign', { defaultCorsPreflightOptions: corsOptions });
    presignResource.addMethod('POST', new apigw.LambdaIntegration(attachmentPresign.fn), methodOptions);

    // Gateway-generated errors (no Lambda ran) use the same envelope as the Lambdas. The static CORS origin is the
    // site; a localhost dev origin therefore sees these particular errors as CORS failures.
    const envelope = (code: string, message: string, retriable: boolean) =>
      `{"error":{"code":"${code}","message":"${message}","request_id":"$context.requestId","retriable":${retriable}}}`;
    const gatewayResponses: Array<[string, apigw.ResponseType, string, string, string, boolean]> = [
      ['Unauthorized', apigw.ResponseType.UNAUTHORIZED, '401', 'unauthorized', 'Authentication is required.', false],
      ['AccessDenied', apigw.ResponseType.ACCESS_DENIED, '403', 'forbidden', 'Access is denied.', false],
      ['Throttled', apigw.ResponseType.THROTTLED, '429', 'rate_limited', 'Too many requests. Try again shortly.', true],
      ['Default4xx', apigw.ResponseType.DEFAULT_4XX, '', 'invalid_request', 'The request could not be processed.', false],
      ['Default5xx', apigw.ResponseType.DEFAULT_5XX, '', 'internal_error', "It's not you, it's us. Something went wrong", false],
    ];
    for (const [name, type, statusCode, code, message, retriable] of gatewayResponses) {
      api.addGatewayResponse(`${name}Response`, {
        type,
        ...(statusCode ? { statusCode } : {}),
        responseHeaders: {
          'Access-Control-Allow-Origin': `'${siteUrl}'`,
          'Access-Control-Allow-Headers': "'Authorization,Content-Type,Accept'",
          'Access-Control-Allow-Methods': "'GET,POST,PATCH,PUT,OPTIONS'",
          Vary: "'Origin'",
        },
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
    // F4: tool Gateway — 10 data-source targets, Router M2M scopes, Cedar (LOG_ONLY -> ENFORCE), token refresh
    // ---------------------------------------------------------------

    const activeRunsTable = new dynamodb.Table(this, 'ActiveRunsStore', {
      partitionKey: { name: 'user_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'record', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: RemovalPolicy.RETAIN,
    });
    // F16: Job ID Store, /jobs routes and the watchdog
    const backgroundJobs = new BackgroundJobs(this, 'BackgroundJobs', {
      api,
      methodOptions,
      corsOptions,
      requestValidator,
      pythonDepsLayer: pythonDeps.layer,
      activeRunsTable,
      allowedOrigins,
      runtimeStopUrls: agentRuntimes.stopUrls,
    });
    agentRuntimes.configureJobStore(backgroundJobs.table, activeRunsTable);
    // F8: GET /models, GET/PUT /settings — UserMetadataTable's first real use (data-model.md Section 4)
    const modelSettings = new ModelSettings(this, 'ModelSettings', {
      api,
      methodOptions,
      corsOptions,
      requestValidator,
      pythonDepsLayer: pythonDeps.layer,
      allowedOrigins,
      blockedModelIds: this.config?.blockedModelIds ?? [],
    });
    // F08: POST /sessions reads the caller's saved default_model_id when none is given (read-only, no PutItem)
    sessionMetadata.fn.addEnvironment('CEI_USER_METADATA_TABLE', modelSettings.table.tableName);
    modelSettings.table.grant(sessionMetadata.role, 'dynamodb:GetItem');
    // F8 follow-up: resolve_turn_config's saved-default lookup for a chat with no stored model_id (read-only)
    agentRuntimes.configureUserMetadataTable(modelSettings.table);
    const gateway = new ToolGateway(this, 'ToolGateway', {
      userPool,
      cognitoDomainUrl,
      userPoolDomain,
      api,
      requestValidator,
      pythonDepsLayer: pythonDeps.layer,
      policyMode: this.config?.toolPolicyMode ?? DEFAULT_TOOL_POLICY_MODE,
      activeRunsTable,
      metadataTable: chatMetadataTable,
      jobTable: backgroundJobs.table,
      spaClientId: spaClient.userPoolClientId,
      refreshCallerRoleArns: [agentRuntimes.runtimes.Research.role.roleArn],
      // B44/B45: preflight.ts already folds the Region gate into this value (see config.webSearchEnabled doc)
      webSearchEnabled: this.config?.webSearchEnabled,
    });
    agentRuntimes.configureResearchGateway(gateway.routerClient.userPoolClientId, gateway.gateway.attrGatewayUrl);
    // B67: only the Research runtime may invoke the internal IAM-authenticated
    // refresh method; the refresh handler also verifies the caller and active run.
    agentRuntimes.runtimes.Research.role.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['execute-api:Invoke'],
      resources: [api.arnForExecuteApi('POST', '/internal/token-refresh', 'v1')],
    }));

    const router = new SessionRouter(this, 'SessionRouter', {
      defaultModelId: this.config?.defaultModelId,
      blockedModelIds: this.config?.blockedModelIds,
      // F9/B17: fixed classifier model, independent of the user's own chat model selection.
      routerModelId: this.config?.routerModelId,
      guardrailId: modelGuardrail.guardrailId,
      guardrailVersion: modelGuardrail.guardrailVersion,
      guardrailArn: modelGuardrail.guardrailArn,
      role: gateway.routerRole,
      pythonDepsLayer: pythonDeps.layer,
      metadataTable: chatMetadataTable,
      activeRunsTable,
      userPoolId: userPool.userPoolId,
      spaClientId: spaClient.userPoolClientId,
      tokenEndpoint: `${cognitoDomainUrl}/oauth2/token`,
      clientId: gateway.routerClient.userPoolClientId,
      secretArn: gateway.routerSecret.secretArn,
      scopes: gateway.scopeNames.map(fullScope),
      runtimeUrls: {
        research: agentRuntimes.invocationUrls.research,
        drafting: agentRuntimes.invocationUrls.drafting,
        analysis: agentRuntimes.invocationUrls.analysis,
        pipeline_manager: agentRuntimes.invocationUrls.pipeline,
      },
    });
    const runnerLogs = new logs.LogGroup(this, 'SessionRunnerLogs', { retention: logs.RetentionDays.ONE_MONTH, removalPolicy: RemovalPolicy.DESTROY });
    const runnerRole = new iam.Role(this, 'SessionRunnerRole', { assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com') });
    runnerLogs.grantWrite(runnerRole);
    runnerRole.addToPrincipalPolicy(new iam.PolicyStatement({ actions: ['dynamodb:GetItem'], resources: [chatMetadataTable.tableArn] }));
    runnerRole.addToPrincipalPolicy(new iam.PolicyStatement({ actions: ['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:Query', 'dynamodb:TransactWriteItems'], resources: [chatHistoryTable.tableArn] }));
    runnerRole.addToPrincipalPolicy(new iam.PolicyStatement({ actions: ['dynamodb:PutItem', 'dynamodb:TransactWriteItems'], resources: [activeRunsTable.tableArn] }));
    runnerRole.addToPrincipalPolicy(new iam.PolicyStatement({ actions: ['lambda:InvokeFunction'], resources: [router.function.functionArn] }));
    // B109/F19: the run-start gate check (GetItem only) stays wired even when accountDeletionEnabled is
    // false -- it is a read with no effect until a gate row can ever exist, so it is always safe to leave live.
    modelSettings.table.grant(runnerRole, 'dynamodb:GetItem');
    const runner = new lambda.Function(this, 'SessionRunner', {
      tracing: lambda.Tracing.ACTIVE,
      runtime: lambda.Runtime.NODEJS_24_X,
      handler: 'session-runner/index.handler',
      code: nodeLambdaCode('session-runner'),
      role: runnerRole,
      logGroup: runnerLogs,
      timeout: Duration.minutes(14),
      memorySize: 512,
      environment: { CEI_CHAT_METADATA_TABLE: chatMetadataTable.tableName, CEI_CHAT_HISTORY_TABLE: chatHistoryTable.tableName, CEI_ACTIVE_RUNS_TABLE: activeRunsTable.tableName, CEI_USER_METADATA_TABLE: modelSettings.table.tableName, CEI_ROUTER_FUNCTION: router.function.functionName, ALLOWED_ORIGINS: allowedOrigins.join(',') },
    });
    suppressXrayWildcard(runnerRole);

    // F13: POST /ui-actions — a typed HITL action resumes the session's own runtime with the caller's token (B102).
    // ADR: STREAM mode | Rationale: a resumed analysis runs for minutes, beyond a buffered integration's 29 s
    // limit; the Node Lambda writes one JSON body | Alternative: buffered + async self-invoke rejected (the user
    // token would sit in the async event queue)
    const uiActionsLogs = new logs.LogGroup(this, 'UiActionsLogs', { retention: logs.RetentionDays.ONE_MONTH, removalPolicy: RemovalPolicy.DESTROY });
    const uiActionsRole = new iam.Role(this, 'UiActionsRole', { assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com') });
    uiActionsLogs.grantWrite(uiActionsRole);
    uiActionsRole.addToPrincipalPolicy(new iam.PolicyStatement({ actions: ['dynamodb:GetItem'], resources: [chatMetadataTable.tableArn] }));
    // F9: the route-choice action reads then clears its own pending-choice record (handler.py writes it)
    // and starts the chosen agent through the Router, same lease/token broker the Runner uses.
    uiActionsRole.addToPrincipalPolicy(new iam.PolicyStatement({ actions: ['dynamodb:GetItem', 'dynamodb:DeleteItem'], resources: [activeRunsTable.tableArn] }));
    uiActionsRole.addToPrincipalPolicy(new iam.PolicyStatement({ actions: ['lambda:InvokeFunction'], resources: [router.function.functionArn] }));
    // B109/F19: same always-live, read-only run-start gate check as the Session Runner above.
    modelSettings.table.grant(uiActionsRole, 'dynamodb:GetItem');
    const uiActions = new lambda.Function(this, 'UiActions', {
      tracing: lambda.Tracing.ACTIVE,
      runtime: lambda.Runtime.NODEJS_24_X,
      handler: 'ui-actions/index.handler',
      code: nodeLambdaCode('ui-actions'),
      role: uiActionsRole,
      logGroup: uiActionsLogs,
      timeout: Duration.minutes(14),
      memorySize: 256,
      environment: {
        CEI_CHAT_METADATA_TABLE: chatMetadataTable.tableName,
        CEI_ACTIVE_RUNS_TABLE: activeRunsTable.tableName,
        CEI_USER_METADATA_TABLE: modelSettings.table.tableName,
        CEI_ROUTER_FUNCTION: router.function.functionName,
        AGENT_URLS: this.toJsonString(agentRuntimes.invocationUrls),
        ALLOWED_ORIGINS: allowedOrigins.join(','),
      },
    });
    suppressXrayWildcard(uiActionsRole);
    const uiActionsResource = api.root.addResource('ui-actions', { defaultCorsPreflightOptions: corsOptions });
    uiActionsResource.addMethod('POST', new apigw.LambdaIntegration(uiActions, { responseTransferMode: apigw.ResponseTransferMode.STREAM, timeout: Duration.minutes(14) }), methodOptions);
    for (const child of uiActionsResource.node.findAll()) {
      if (child instanceof apigw.CfnMethod && child.httpMethod === 'OPTIONS') {
        child.requestValidatorId = requestValidator.requestValidatorId;
        NagSuppressions.addResourceSuppressions(child, [
          { id: 'AwsSolutions-APIG4', reason: 'ADR: CORS preflight without authorizer | Rationale: browsers do not send credentials on preflight | Alternative: authenticated preflight rejected because browsers cannot satisfy it' },
          { id: 'AwsSolutions-COG4', reason: 'ADR: CORS preflight without Cognito | Rationale: browsers do not send credentials on preflight | Alternative: authenticated preflight rejected because browsers cannot satisfy it' },
        ]);
      }
    }

    // ---------------------------------------------------------------
    // F19/B109: POST /account/delete + the scheduled delete-worker. Off by default; no route, no
    // Lambdas, no Scheduler resources, and no Cognito/DynamoDB/S3/Secrets/Memory delete grants exist
    // unless accountDeletionEnabled is true. The gate *check* in every other Lambda above stays live
    // either way (a harmless read).
    // ---------------------------------------------------------------
    let accountDeletion: AccountDeletion | undefined;
    if (this.config?.accountDeletionEnabled) {
      const apiKeySourceIds = [...loadToolSchemas().map((s) => s.sourceId), ...RESERVED_SOURCE_IDS];
      accountDeletion = new AccountDeletion(this, 'AccountDeletion', {
        api,
        methodOptions,
        corsOptions,
        requestValidator,
        pythonDepsLayer: pythonDeps.layer,
        allowedOrigins,
        userPool,
        userMetadataTable: modelSettings.table,
        chatMetadataTable,
        chatHistoryTable,
        activeRunsTable,
        exportsTable,
        exportsBucket,
        jobTable: backgroundJobs.table,
        apiKeySecretPrefix: 'cei/user-keys',
        apiKeySourceIds,
        memoryId: agentRuntimes.memoryId,
        memoryArn: agentRuntimes.memoryArn,
      });
    }

    new Observability(this, 'Observability', {
      api,
      adminEmail: this.config?.adminEmail,
      functions: {
        PreSignUp: preSignUpFn, SessionMetadata: sessionMetadata.fn, SessionHistory: sessionHistory.fn,
        AgentProxy: agentProxyFn, SessionRouter: router.function, SessionRunner: runner, UiActions: uiActions,
        ClientDiagnostics: clientDiagnostics.fn, Exports: exportsHandler.fn, LifeSciencesOperations: gateway.operationsFunction,
        NcbiReadOnly: gateway.ncbiFunction, Jobs: backgroundJobs.jobsFunction, JobWatchdog: backgroundJobs.watchdogFunction,
        PreToken: gateway.preTokenFunction, TokenRefresh: gateway.tokenRefreshFunction, ModelSettings: modelSettings.function,
        AttachmentPresign: attachmentPresign.fn, AttachmentProcessor: attachmentProcessor,
        ...Object.fromEntries(Object.entries(gateway.sourceFunctions).map(([name, fn]) => [`Source${name}`, fn])),
      },
      tables: {
        ChatMetadata: chatMetadataTable, ChatHistory: chatHistoryTable, ActiveRuns: activeRunsTable,
        Exports: exportsTable, NcbiRateLimiter: gateway.ncbiRateTable, JobIdStore: backgroundJobs.table,
        UserMetadata: modelSettings.table, Attachments: attachmentsTable
      },
      signupLogs: preSignUpLogGroup,
      gatewayLogs: gateway.gatewayLogGroup,
      clientLogs: clientDiagnostics.fn.logGroup,
      agentProxyLogs: agentProxyLogGroup,
      attachmentProcessorDlq,
      caughtFailures: {
        SessionRunner: { logGroup: runnerLogs, terms: ['request_failed', 'stream_failed', 'agent_error', 'write_failed', 'stream_transport_failed', 'replay_timeout', 'persistence_failed', 'cleanup_failed'] },
        SessionRouter: { logGroup: router.function.logGroup, terms: ['failed'] },
        AgentProxy: { logGroup: agentProxyLogGroup, terms: ['agent_proxy_failure', 'agent_proxy_invalid_json'] },
        SessionMetadata: { logGroup: sessionMetadata.fn.logGroup, terms: ['handled_api_failure', 'unhandled error'] },
        SessionHistory: { logGroup: sessionHistory.fn.logGroup, terms: ['handled_api_failure', 'unhandled error'] },
        Exports: { logGroup: exportsHandler.fn.logGroup, terms: ['handled_api_failure', 'unhandled error'] },
        ModelSettings: { logGroup: modelSettings.function.logGroup, terms: ['handled_api_failure', 'unhandled error'] },
        NcbiReadOnly: { logGroup: gateway.ncbiFunction.logGroup, terms: ['ncbi_request_failed'] },
        UiActions: { logGroup: uiActionsLogs, terms: ['resume_failed'] },
        Jobs: { logGroup: backgroundJobs.jobsFunction.logGroup, terms: ['handled_api_failure', 'unhandled error', 'runtime_stop_failed'] },
        TokenRefresh: { logGroup: gateway.tokenRefreshFunction.logGroup, terms: ['token_endpoint_error', 'token_refresh_scope_mismatch', 'run_scopes_not_allowed', 'unhandled error'] },
        AttachmentPresign: { logGroup: attachmentPresign.fn.logGroup, terms: ['presign failed', 'metadata write failed'] },
        AttachmentProcessor: { logGroup: processorLogGroup, terms: ['Failed', 'extraction failed'] },
        ...Object.fromEntries(Object.entries(gateway.sourceFunctions).map(([name, fn]) =>
          [`Source${name}`, { logGroup: fn.logGroup, terms: ['upstream_error', 'exception', 'api_key_lookup_failed', ...(name === 'cmscoverage' ? ['cms_ncd_detail_error'] : [])] }])),
      },
    });
    const chatResource = api.root.addResource('chat', { defaultCorsPreflightOptions: corsOptions });
    const streamResource = chatResource.addResource('stream', { defaultCorsPreflightOptions: corsOptions });
    streamResource.addMethod('POST', new apigw.LambdaIntegration(runner, { responseTransferMode: apigw.ResponseTransferMode.STREAM, timeout: Duration.minutes(14) }), methodOptions);
    for (const child of chatResource.node.findAll()) {
      if (child instanceof apigw.CfnMethod && child.httpMethod === 'OPTIONS') {
        child.requestValidatorId = requestValidator.requestValidatorId;
        NagSuppressions.addResourceSuppressions(child, [
          { id: 'AwsSolutions-APIG4', reason: 'ADR: CORS preflight without authorizer | Rationale: browsers do not send credentials on preflight | Alternative: authenticated preflight rejected because browsers cannot satisfy it' },
          { id: 'AwsSolutions-COG4', reason: 'ADR: CORS preflight without Cognito | Rationale: browsers do not send credentials on preflight | Alternative: authenticated preflight rejected because browsers cannot satisfy it' },
        ]);
      }
    }

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

    // ADR: kept as a named, readable-after-construction object (not inlined into Source.jsonData) |
    // Rationale: Source.jsonData stages this into a zipped S3 asset that Jest cannot inspect without
    // unzipping synthesized output; exposing it as `this.runtimeConfig` lets tests assert on the exact
    // value shipped to the SPA's /config.json (for example accountDeletionEnabled, F19/B109) directly
    this.runtimeConfig = {
      region: this.region,
      userPoolId: userPool.userPoolId,
      userPoolClientId: spaClient.userPoolClientId,
      cognitoDomain: cognitoDomainUrl,
      apiBaseUrl,
      // The streaming Research path relies on Cedar to enforce source scopes.
      researchStreamingEnabled: (this.config?.toolPolicyMode ?? DEFAULT_TOOL_POLICY_MODE) === 'ENFORCE',
      // F19/B109: whether the SPA may call POST /account/delete at all; false when the route does not exist.
      accountDeletionEnabled: Boolean(this.config?.accountDeletionEnabled),
      // Agent proxy function URL per agent (the proxy forwards to the AgentCore invocation URL)
      agentUrls: agentProxyUrls,
    };

    const configDeployment = new s3deploy.BucketDeployment(this, 'RuntimeConfigDeployment', {
      sources: [s3deploy.Source.jsonData('config.json', this.runtimeConfig)],
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
    new cdk.CfnOutput(this, 'AgentProxyUrl', {
      value: agentProxyUrl.url,
      description: 'Agent proxy function URL; the SPA POSTs to <url><agent> (research | drafting | analysis | pipeline)',
    });
    // No exportName: a stack export would lock the generated bucket name; copy this into backend/agents/.env for local dev
    new cdk.CfnOutput(this, 'AttachmentsBucketName', {
      value: attachmentsBucket.bucketName,
      description: 'CDK-created attachments bucket (set CEI_ATTACHMENTS_BUCKET to this for local agent runs)',
    });

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
