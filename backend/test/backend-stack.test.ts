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
      // ADR: not resourceCountIs(1) | Rationale: BucketDeployment adds its own CDK-owned singleton Lambda handler alongside the pre-sign-up trigger
      template.resourcePropertiesCountIs(
        'AWS::Lambda::Function',
        Match.objectLike({ Handler: 'handler.handler' }),
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

    it('has the SPA fallback custom error responses', () => {
      const app = new cdk.App();
      const stack = testStack(app, 'TestStack');
      const template = Template.fromStack(stack);
      template.hasResourceProperties('AWS::CloudFront::Distribution', {
        DistributionConfig: Match.objectLike({
          CustomErrorResponses: Match.arrayWith([
            Match.objectLike({ ErrorCode: 403, ResponseCode: 200, ResponsePagePath: '/index.html' }),
            Match.objectLike({ ErrorCode: 404, ResponseCode: 200, ResponsePagePath: '/index.html' }),
          ]),
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
      ]) {
        expect(Object.keys(template.findOutputs(outputName))).toHaveLength(1);
      }
    });
  });
});
