// FIXME: remove after S2 validation (B92)
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Annotations, Match, Template } from 'aws-cdk-lib/assertions';
import { buildStacks } from '../lib/app';
import { DEFAULT_LIMITS, DeployConfig } from '../lib/config/deploy-config';

const FIXTURE_ASSET_PATH = path.join(__dirname, 'fixtures', 'spa');

function testConfig(): DeployConfig {
  return {
    region: 'eu-west-1',
    defaultModelId: 'anthropic.claude-3-5-sonnet-20241022-v2:0',
    routerModelId: 'anthropic.claude-3-haiku-20240307-v1:0',
    blockedModelIds: [],
    allowedEmailDomains: ['example.com'],
    adminEmail: 'admin@example.com',
    limits: DEFAULT_LIMITS,
    webSearchEnabled: true,
    allowLocalhostCallback: false,
  };
}

function build(context: Record<string, string> = {}) {
  process.env.CDK_DEFAULT_ACCOUNT = '123456789012';
  const app = new cdk.App({ context });
  return { app, built: buildStacks(app, testConfig(), FIXTURE_ASSET_PATH) };
}

describe('ToolScopeSpikeStack wiring (B92)', () => {
  it('is absent without the ceiSpikes flag', () => {
    const { app, built } = build();
    expect(built.toolScopeSpike).toBeUndefined();
    expect(app.node.tryFindChild('ToolScopeSpikeStack')).toBeUndefined();
  });

  it('is absent when the flag lists only other spikes', () => {
    const { built } = build({ ceiSpikes: 'something-else' });
    expect(built.toolScopeSpike).toBeUndefined();
  });

  it('is created in the deploy Region when the flag contains tool-scope', () => {
    const { built } = build({ ceiSpikes: 'other, tool-scope' });
    expect(built.toolScopeSpike).toBeDefined();
    expect(built.toolScopeSpike!.region).toBe('eu-west-1');
  });

  it('rejects an unknown policy mode', () => {
    expect(() => build({ ceiSpikes: 'tool-scope', ceiSpikePolicyMode: 'OFF' })).toThrow(/LOG_ONLY or ENFORCE/);
  });
});

describe('ToolScopeSpikeStack', () => {
  const { built } = build({ ceiSpikes: 'tool-scope' });
  const stack = built.toolScopeSpike!;
  const template = Template.fromStack(stack);

  it('defaults the Gateway policy mode to LOG_ONLY and honours ceiSpikePolicyMode', () => {
    template.hasResourceProperties('AWS::BedrockAgentCore::Gateway', {
      PolicyEngineConfiguration: { Mode: 'LOG_ONLY', Arn: Match.anyValue() },
    });
    const enforce = Template.fromStack(build({ ceiSpikes: 'tool-scope', ceiSpikePolicyMode: 'ENFORCE' }).built.toolScopeSpike!);
    enforce.hasResourceProperties('AWS::BedrockAgentCore::Gateway', {
      PolicyEngineConfiguration: { Mode: 'ENFORCE', Arn: Match.anyValue() },
    });
  });

  it('uses a custom JWT authorizer on the spike pool and three Lambda targets', () => {
    template.hasResourceProperties('AWS::BedrockAgentCore::Gateway', {
      AuthorizerType: 'CUSTOM_JWT',
      AuthorizerConfiguration: { CustomJWTAuthorizer: Match.objectLike({ AllowedClients: Match.anyValue() }) },
    });
    template.resourceCountIs('AWS::BedrockAgentCore::GatewayTarget', 3);
  });

  it('defines scopes where no name is a substring of another (B69)', () => {
    const servers = template.findResources('AWS::Cognito::UserPoolResourceServer');
    const server = Object.values(servers)[0].Properties;
    const names: string[] = server.Scopes.map((s: { ScopeName: string }) => s.ScopeName);
    expect(names).toEqual(expect.arrayContaining(['target-a', 'target-b', 'shared']));
    for (const a of names) {
      for (const b of names) {
        if (a !== b) expect(b.includes(a)).toBe(false);
      }
    }
  });

  it('writes exactly one permit per target, scoped at target level (B69)', () => {
    const policies = Object.values(template.findResources('AWS::BedrockAgentCore::Policy'));
    expect(policies).toHaveLength(3);
    const statements = policies.map((p) => JSON.stringify(p.Properties.Definition.Cedar.Statement));
    for (const target of ['SpikeTargetA', 'SpikeTargetB', 'SpikeTargetShared']) {
      const matching = statements.filter((s) => s.includes(`AgentCore::Action::\\"${target}\\"`));
      expect(matching).toHaveLength(1);
      expect(matching[0]).toContain('permit(');
    }
    // No blanket forbid: Cedar is already default deny and a forbid would override every permit.
    expect(statements.some((s) => s.includes('forbid'))).toBe(false);
  });

  it('gates target-a and target-b on their scope and leaves the shared target open (B70)', () => {
    const statements = Object.values(template.findResources('AWS::BedrockAgentCore::Policy')).map((p) =>
      JSON.stringify(p.Properties.Definition.Cedar.Statement),
    );
    const find = (t: string) => statements.find((s) => s.includes(`\\"${t}\\"`))!;
    expect(find('SpikeTargetA')).toContain('cei-tools-spike/target-a');
    expect(find('SpikeTargetB')).toContain('cei-tools-spike/target-b');
    expect(find('SpikeTargetShared')).not.toContain('getTag');
  });

  it('limits secret reads to the runner role via an explicit resource-policy Deny', () => {
    template.hasResourceProperties('AWS::SecretsManager::ResourcePolicy', {
      ResourcePolicy: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Effect: 'Deny',
            Action: 'secretsmanager:GetSecretValue',
            Principal: { AWS: '*' },
            Condition: { StringNotEquals: { 'aws:PrincipalArn': Match.anyValue() } },
          }),
        ]),
      },
    });

    // Only the runner role gets an identity grant that names the secret.
    const roleIdFor = (prefix: string) =>
      Object.keys(template.findResources('AWS::IAM::Role')).find((id) => id.startsWith(prefix))!;
    const runnerId = roleIdFor('SpikeRunnerRole');
    const runtimeId = roleIdFor('SpikeRuntimeRole');
    const secretId = Object.keys(template.findResources('AWS::SecretsManager::Secret'))[0];
    const grantsSecret = (roleId: string) =>
      Object.values(template.findResources('AWS::IAM::Policy')).some((pol) => {
        const attachedTo = (pol.Properties.Roles as Array<{ Ref: string }>).some((r) => r.Ref === roleId);
        return attachedTo && JSON.stringify(pol.Properties.PolicyDocument).includes(secretId);
      });
    expect(grantsSecret(runnerId)).toBe(true);
    expect(grantsSecret(runtimeId)).toBe(false);
  });

  it('exposes an IAM-authenticated token-refresh Function URL', () => {
    template.hasResourceProperties('AWS::Lambda::Url', { AuthType: 'AWS_IAM' });
  });

  it('removes everything with the stack (throwaway, DESTROY)', () => {
    for (const type of ['AWS::SecretsManager::Secret', 'AWS::Cognito::UserPool', 'AWS::Logs::LogGroup']) {
      for (const res of Object.values(template.findResources(type))) {
        expect(res.DeletionPolicy).toBe('Delete');
      }
    }
  });

  it('produces zero unsuppressed cdk-nag AwsSolutions errors or warnings', () => {
    const errors = Annotations.fromStack(stack).findError('*', Match.stringLikeRegexp('AwsSolutions-.*'));
    const warnings = Annotations.fromStack(stack).findWarning('*', Match.stringLikeRegexp('AwsSolutions-.*'));
    expect(errors).toEqual([]);
    expect(warnings).toEqual([]);
  });
});
