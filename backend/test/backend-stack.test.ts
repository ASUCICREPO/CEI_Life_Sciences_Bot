import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { Annotations } from 'aws-cdk-lib/assertions';
import { BackendStack, BackendStackProps } from '../lib/backend-stack';
import { DeployConfig, DEFAULT_LIMITS } from '../lib/config/deploy-config';

const FIXTURE_ASSET_PATH = path.join(__dirname, 'fixtures', 'spa');

function testConfig(overrides: Partial<DeployConfig> = {}): DeployConfig {
  return {
    region: 'us-east-1',
    defaultModelId: 'anthropic.claude-3-5-sonnet-20241022-v2:0',
    routerModelId: 'anthropic.claude-3-haiku-20240307-v1:0',
    blockedModelIds: [],
    allowedEmailDomains: ['example.com'],
    adminEmail: 'admin@example.com',
    limits: DEFAULT_LIMITS,
    webSearchEnabled: true,
    allowLocalhostCallback: false,
    ...overrides,
  };
}

function testStack(app: cdk.App, id: string, overrides: Partial<BackendStackProps> = {}): BackendStack {
  return new BackendStack(app, id, {
    env: { region: 'us-east-1', account: '123456789012' },
    frontendAssetPath: FIXTURE_ASSET_PATH,
    ...overrides,
  });
}

describe('BackendStack', () => {
  it('creates the AgentCore Code Interpreter', () => {
    const app = new cdk.App();
    const stack = testStack(app, 'TestStack');
    const template = Template.fromStack(stack);
    template.resourceCountIs('AWS::BedrockAgentCore::CodeInterpreterCustom', 1);
    template.hasResourceProperties('AWS::BedrockAgentCore::CodeInterpreterCustom', {
      Name: 'ceiLifeSciencesCI',
    });
  });

  it('has no WebSearchEnabled output without a config', () => {
    const app = new cdk.App();
    const stack = testStack(app, 'TestStack');
    const template = Template.fromStack(stack);
    expect(Object.keys(template.findOutputs('WebSearchEnabled'))).toHaveLength(0);
  });

  it('exposes config and a WebSearchEnabled output when a config is supplied', () => {
    const app = new cdk.App();
    const config = testConfig({ webSearchEnabled: true });
    const stack = testStack(app, 'TestStack', { config });
    expect(stack.config).toBe(config);
    const template = Template.fromStack(stack);
    template.hasOutput('WebSearchEnabled', { Value: 'true' });
  });

  it('tags the stack with Project', () => {
    const app = new cdk.App();
    const stack = testStack(app, 'TestStack');
    const template = Template.fromStack(stack);
    template.hasResourceProperties(
      'AWS::BedrockAgentCore::CodeInterpreterCustom',
      Match.objectLike({
        Tags: Match.objectLike({ Project: 'CEI-Life-Sciences-Bot' }),
      }),
    );
  });

  it('produces zero unsuppressed cdk-nag AwsSolutions errors without a config', () => {
    const app = new cdk.App();
    const stack = testStack(app, 'TestStack');
    Template.fromStack(stack);
    const errors = Annotations.fromStack(stack).findError('*', Match.stringLikeRegexp('AwsSolutions-.*'));
    expect(errors).toHaveLength(0);
  });

  it('produces zero unsuppressed cdk-nag AwsSolutions errors with a config', () => {
    const app = new cdk.App();
    const stack = testStack(app, 'TestStack', {
      config: testConfig(),
      webAclArn: 'arn:aws:wafv2:us-east-1:123456789012:global/webacl/edge/abc123',
    });
    Template.fromStack(stack);
    const errors = Annotations.fromStack(stack).findError('*', Match.stringLikeRegexp('AwsSolutions-.*'));
    expect(errors).toHaveLength(0);
  });

  describe('Cognito user pool', () => {
    it('requires email verification and disables MFA', () => {
      const app = new cdk.App();
      const stack = testStack(app, 'TestStack');
      const template = Template.fromStack(stack);
      template.hasResourceProperties('AWS::Cognito::UserPool', {
        AutoVerifiedAttributes: ['email'],
        MfaConfiguration: 'OFF',
      });
    });

    it('attaches the pre-sign-up trigger', () => {
      const app = new cdk.App();
      const stack = testStack(app, 'TestStack');
      const template = Template.fromStack(stack);
      template.hasResourceProperties('AWS::Cognito::UserPool', {
        LambdaConfig: Match.objectLike({ PreSignUp: Match.anyValue() }),
      });
      // ADR: not resourceCountIs(1) | Rationale: BucketDeployment adds its own CDK-owned singleton Lambda handler, and the agent proxy also uses handler.handler
      template.resourcePropertiesCountIs(
        'AWS::Lambda::Function',
        Match.objectLike({
          Handler: 'handler.handler',
          Environment: { Variables: Match.objectLike({ ALLOWED_EMAIL_DOMAINS: Match.anyValue() }) },
        }),
        1,
      );
    });

    it('creates the SPA client with no secret and authorization-code-grant only', () => {
      const app = new cdk.App();
      const stack = testStack(app, 'TestStack');
      const template = Template.fromStack(stack);
      template.hasResourceProperties('AWS::Cognito::UserPoolClient', {
        GenerateSecret: false,
        AllowedOAuthFlows: ['code'],
        AllowedOAuthFlowsUserPoolClient: true,
      });
    });

    it('adds localhost callback/logout URLs only when allowLocalhostCallback is set', () => {
      const app = new cdk.App();
      const stack = testStack(app, 'TestStack', {
        config: testConfig({ allowLocalhostCallback: true }),
      });
      const template = Template.fromStack(stack);
      template.hasResourceProperties('AWS::Cognito::UserPoolClient', {
        CallbackURLs: Match.arrayWith(['http://localhost:5173/auth/callback']),
        LogoutURLs: Match.arrayWith(['http://localhost:5173/']),
      });
    });

    it('omits localhost callback/logout URLs without allowLocalhostCallback', () => {
      const app = new cdk.App();
      const stack = testStack(app, 'TestStack', { config: testConfig() });
      const template = Template.fromStack(stack);
      const clients = template.findResources('AWS::Cognito::UserPoolClient');
      const client = Object.values(clients)[0];
      expect(client.Properties.CallbackURLs).not.toContain('http://localhost:5173/auth/callback');
    });

    it('makes the email attribute required and immutable', () => {
      const app = new cdk.App();
      const stack = testStack(app, 'TestStack');
      const template = Template.fromStack(stack);
      template.hasResourceProperties('AWS::Cognito::UserPool', {
        Schema: Match.arrayWith([
          Match.objectLike({ Name: 'email', Required: true, Mutable: false }),
        ]),
      });
    });

    it('locks the SPA client to exactly ALLOW_REFRESH_TOKEN_AUTH (hosted-UI/PKCE only, no direct-auth flows)', () => {
      const app = new cdk.App();
      const stack = testStack(app, 'TestStack');
      const template = Template.fromStack(stack);
      const clients = template.findResources('AWS::Cognito::UserPoolClient');
      const client = Object.values(clients)[0];
      expect(client.Properties.ExplicitAuthFlows).toEqual(['ALLOW_REFRESH_TOKEN_AUTH']);
    });

    it('gives the SPA client no write access to any attribute (email is immutable at the schema level)', () => {
      const app = new cdk.App();
      const stack = testStack(app, 'TestStack');
      const template = Template.fromStack(stack);
      const clients = template.findResources('AWS::Cognito::UserPoolClient');
      const client = Object.values(clients)[0];
      expect(client.Properties.WriteAttributes ?? []).toEqual([]);
      expect(client.Properties.ReadAttributes).toEqual(expect.arrayContaining(['email']));
    });
  });

  describe('Hosting', () => {
    it('creates a private S3 bucket with OAC-fronted CloudFront', () => {
      const app = new cdk.App();
      const stack = testStack(app, 'TestStack');
      const template = Template.fromStack(stack);
      template.hasResourceProperties('AWS::S3::Bucket', {
        PublicAccessBlockConfiguration: {
          BlockPublicAcls: true,
          BlockPublicPolicy: true,
          IgnorePublicAcls: true,
          RestrictPublicBuckets: true,
        },
      });
      template.resourceCountIs('AWS::CloudFront::OriginAccessControl', 1);
    });

    it('uses a viewer-request SPA fallback without rewriting API errors', () => {
      const app = new cdk.App();
      const stack = testStack(app, 'TestStack');
      const template = Template.fromStack(stack);
      template.hasResourceProperties('AWS::CloudFront::Distribution', {
        DistributionConfig: Match.objectLike({
          CustomErrorResponses: Match.absent(),
          DefaultCacheBehavior: Match.objectLike({ FunctionAssociations: Match.arrayWith([Match.objectLike({ EventType: 'viewer-request' })]) }),
        }),
      });
    });

    it('sets a Permissions-Policy and Cross-Origin-Opener-Policy header', () => {
      const app = new cdk.App();
      const stack = testStack(app, 'TestStack');
      const template = Template.fromStack(stack);
      template.hasResourceProperties('AWS::CloudFront::ResponseHeadersPolicy', {
        ResponseHeadersPolicyConfig: Match.objectLike({
          CustomHeadersConfig: {
            Items: Match.arrayWith([
              Match.objectLike({ Header: 'Permissions-Policy' }),
              Match.objectLike({ Header: 'Cross-Origin-Opener-Policy', Value: 'same-origin' }),
            ]),
          },
        }),
      });
    });

    it('expires access log objects after 90 days', () => {
      const app = new cdk.App();
      const stack = testStack(app, 'TestStack');
      const template = Template.fromStack(stack);
      template.hasResourceProperties('AWS::S3::Bucket', {
        LifecycleConfiguration: Match.objectLike({
          Rules: Match.arrayWith([Match.objectLike({ ExpirationInDays: 90, Status: 'Enabled' })]),
        }),
      });
    });

    it('attaches the web ACL when webAclArn is supplied', () => {
      const app = new cdk.App();
      const webAclArn = 'arn:aws:wafv2:us-east-1:123456789012:global/webacl/edge/abc123';
      const stack = testStack(app, 'TestStack', { config: testConfig(), webAclArn });
      const template = Template.fromStack(stack);
      template.hasResourceProperties('AWS::CloudFront::Distribution', {
        DistributionConfig: Match.objectLike({ WebACLId: webAclArn }),
      });
    });

    it('exposes the hosting/auth CfnOutputs', () => {
      const app = new cdk.App();
      const stack = testStack(app, 'TestStack', { config: testConfig() });
      const template = Template.fromStack(stack);
      for (const outputName of [
        'UserPoolId',
        'UserPoolArn',
        'UserPoolClientId',
        'CognitoDomain',
        'DistributionDomainName',
        'SiteUrl',
        'ApiBaseUrl',
        'AgentProxyUrl',
        'ResearchRuntimeArn',
        'DraftingRuntimeArn',
        'AnalysisRuntimeArn',
        'PipelineRuntimeArn',
      ]) {
        expect(Object.keys(template.findOutputs(outputName))).toHaveLength(1);
      }
    });

    it('ships the four agent proxy URLs in config.json, not raw AgentCore URLs', () => {
      const app = new cdk.App();
      const template = Template.fromStack(testStack(app, 'TestStack', { config: testConfig() }));
      const deployments = Object.values(template.findResources('Custom::CDKBucketDeployment'));
      const configDeployment = deployments.find((d) => JSON.stringify(d).includes('application/json'));
      expect(configDeployment).toBeDefined();
      const serialized = JSON.stringify(configDeployment);
      const proxyUrlLogicalId = Object.keys(template.findResources('AWS::Lambda::Url'))[0];
      expect(proxyUrlLogicalId).toBeDefined();
      expect(serialized).toContain(proxyUrlLogicalId);
      for (const agent of ['research', 'drafting', 'analysis', 'pipeline']) {
        expect(serialized).toContain(agent);
      }
      expect(serialized).not.toContain('/invocations?qualifier=DEFAULT');
    });

    it('fronts the runtimes with a CORS-restricted agent proxy function URL', () => {
      const app = new cdk.App();
      const template = Template.fromStack(testStack(app, 'TestStack', { config: testConfig() }));
      const urls = Object.values(template.findResources('AWS::Lambda::Url'));
      expect(urls).toHaveLength(1);
      const props = urls[0].Properties;
      expect(props.AuthType).toBe('NONE');
      expect(props.InvokeMode).toBe('BUFFERED');
      expect(props.Cors.AllowMethods).toEqual(['POST']);
      expect(props.Cors.AllowCredentials).toBe(false);
      expect(props.Cors.AllowHeaders).toEqual(
        expect.arrayContaining(['authorization', 'content-type', 'x-amzn-bedrock-agentcore-runtime-session-id']),
      );
      const origins = JSON.stringify(props.Cors.AllowOrigins);
      expect(origins).not.toContain('*');
      expect(origins).not.toContain('localhost');
      expect(origins).toContain('DomainName');

      // The proxy Lambda gets the four AgentCore invocation URLs and a timeout above the SPA's 10-minute wait
      const fn = Object.values(template.findResources('AWS::Lambda::Function')).find(
        (f) => f.Properties?.Environment?.Variables?.AGENT_URLS !== undefined,
      );
      expect(fn).toBeDefined();
      expect(fn!.Properties.Timeout).toBe(900);
      expect(JSON.stringify(fn!.Properties.Environment.Variables.AGENT_URLS).match(/invocations\?qualifier=DEFAULT/g)).toHaveLength(4);
    });

    it('allows the agent proxy (region-scoped Lambda URL host) in the CSP connect-src, not AgentCore', () => {
      const app = new cdk.App();
      const template = Template.fromStack(testStack(app, 'TestStack', { config: testConfig() }));
      const policies = Object.values(template.findResources('AWS::CloudFront::ResponseHeadersPolicy'));
      const csp = JSON.stringify(policies);
      expect(csp).toMatch(/connect-src [^;]*https:\/\/\*\.lambda-url\.us-east-1\.on\.aws/);
      expect(csp).not.toContain('bedrock-agentcore');
    });
  });

  describe('Session stores', () => {
    const tables = () => {
      const app = new cdk.App();
      const template = Template.fromStack(testStack(app, 'TestStack'));
      const all = Object.values(template.findResources('AWS::DynamoDB::Table'));
      const byPk = (pk: string) =>
        all.find((t) => t.Properties.KeySchema.some((k: any) => k.KeyType === 'HASH' && k.AttributeName === pk))!;
      return { template, all, metadata: byPk('user_id'), history: byPk('session_id') };
    };

    it('defines the sort and title-search GSIs on ChatMetadataStore with projection ALL', () => {
      const { metadata } = tables();
      const gsis = metadata.Properties.GlobalSecondaryIndexes;
      expect(gsis).toHaveLength(4);
      for (const [name, sk] of [
        ['by_created', 'created_at'],
        ['by_updated', 'updated_at'],
        ['by_activity', 'last_activity_at'],
        ['by_title', 'title_search'],
      ]) {
        const gsi = gsis.find((g: any) => g.IndexName === name);
        expect(gsi.KeySchema).toEqual([
          { AttributeName: 'user_id', KeyType: 'HASH' },
          { AttributeName: sk, KeyType: 'RANGE' },
        ]);
        expect(gsi.Projection).toEqual({ ProjectionType: 'ALL' });
      }
    });

    it('keys ChatHistoryStore by session_id + numeric sequence_number', () => {
      const { history } = tables();
      expect(history.Properties.KeySchema).toEqual([
        { AttributeName: 'session_id', KeyType: 'HASH' },
        { AttributeName: 'sequence_number', KeyType: 'RANGE' },
      ]);
      expect(history.Properties.AttributeDefinitions).toEqual(
        expect.arrayContaining([{ AttributeName: 'sequence_number', AttributeType: 'N' }]),
      );
    });

    it('enables PITR, retains, and sets no TTL on the tables', () => {
      const { all } = tables();
      expect(all).toHaveLength(6);
      for (const t of all) {
        expect(t.Properties.PointInTimeRecoverySpecification).toEqual({ PointInTimeRecoveryEnabled: true });
        expect(t.Properties.BillingMode).toBe('PAY_PER_REQUEST');
        if (t.Properties.KeySchema[0].AttributeName === 'pk') {
          expect(t.Properties.TimeToLiveSpecification).toEqual({ AttributeName: 'expires_at', Enabled: true });
          expect(t.DeletionPolicy).toBe('Delete');
        } else {
          expect(t.Properties.TimeToLiveSpecification).toBeUndefined();
          expect(t.DeletionPolicy).toBe('Retain');
        }
      }
    });

    it('grants the metadata GSIs by exact name, never index/*', () => {
      const { template } = tables();
      const policies = JSON.stringify(template.findResources('AWS::IAM::Policy'));
      for (const name of ['by_created', 'by_updated', 'by_activity', 'by_title']) {
        expect(policies).toContain(`/index/${name}`);
      }
      expect(policies).not.toContain('/index/*');
    });

    it('keeps the original metadata table logical id so the deployed table is never replaced', () => {
      const { template } = tables();
      const ids = Object.keys(template.findResources('AWS::DynamoDB::Table'));
      const metadataIds = ids.filter((id) => id.includes('ChatMetadataStore'));
      expect(metadataIds).toHaveLength(1);
      expect(metadataIds[0]).not.toContain('ChatMetadataStoreV2');
    });

    // Every DynamoDB statement attached to roles whose logical id matches `rolePattern`, flattened per action
    const dynamoGrants = (rolePattern: RegExp) => {
      const { template } = tables();
      const grants: { action: string; resource: string }[] = [];
      for (const policy of Object.values(template.findResources('AWS::IAM::Policy'))) {
        const roles: string[] = (policy.Properties.Roles ?? []).map((r: any) => r.Ref ?? '');
        if (!roles.some((r) => rolePattern.test(r))) continue;
        for (const st of policy.Properties.PolicyDocument.Statement) {
          const actions: string[] = [st.Action].flat();
          const resources: any[] = [st.Resource].flat();
          for (const action of actions.filter((a) => a.startsWith('dynamodb:'))) {
            for (const res of resources) grants.push({ action, resource: JSON.stringify(res) });
          }
        }
      }
      return grants;
    };
    const onMetadataTable = (r: string) => r.includes('ChatMetadataStore') && !r.includes('/index/');
    const onMetadataIndex = (r: string) => r.includes('ChatMetadataStore') && r.includes('/index/');
    const onHistoryTable = (r: string) => r.includes('ChatHistoryStore');

    it('grants the AgentRuntimes roles UpdateItem on metadata, Query on exact index ARNs, and no Scan', () => {
      const grants = dynamoGrants(/^AgentRuntimes/);
      expect(grants.length).toBeGreaterThan(0);
      expect(grants.some((g) => g.action === 'dynamodb:UpdateItem' && onMetadataTable(g.resource))).toBe(true);
      const indexGrants = grants.filter((g) => onMetadataIndex(g.resource));
      expect(indexGrants.every((g) => g.action === 'dynamodb:Query')).toBe(true);
      for (const name of ['by_created', 'by_updated', 'by_activity', 'by_title']) {
        expect(indexGrants.some((g) => g.resource.includes(`/index/${name}`))).toBe(true);
      }
      expect(grants.some((g) => g.resource.includes('/index/*'))).toBe(false);
      expect(grants.some((g) => g.action === 'dynamodb:Scan' || g.action === 'dynamodb:*')).toBe(false);
    });

    it('gives the metadata Lambda no DeleteItem or Scan, and history access only via Query', () => {
      const grants = dynamoGrants(/^SessionMetadataHandlerRole/);
      expect(grants.length).toBeGreaterThan(0);
      for (const forbidden of ['dynamodb:DeleteItem', 'dynamodb:Scan', 'dynamodb:*']) {
        expect(grants.some((g) => g.action === forbidden)).toBe(false);
      }
      expect(grants.filter((g) => onHistoryTable(g.resource)).map((g) => g.action)).toEqual(['dynamodb:Query']);
    });

    it('grants history and trace reads on the correct tables', () => {
      const grants = dynamoGrants(/^SessionHistoryHandlerRole/);
      const metadataActions = grants.filter((g) => g.resource.includes('ChatMetadataStore')).map((g) => g.action);
      expect(metadataActions).toEqual(['dynamodb:GetItem']);
      expect(grants.filter((g) => onHistoryTable(g.resource)).map((g) => g.action).sort()).toEqual(['dynamodb:BatchGetItem', 'dynamodb:GetItem', 'dynamodb:Query']);
    });
  });
});
