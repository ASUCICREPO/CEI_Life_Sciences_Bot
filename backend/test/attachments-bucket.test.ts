import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { buildStacks } from '../lib/app';
import { DeployConfig, DEFAULT_LIMITS } from '../lib/config/deploy-config';

const FIXTURE_ASSET_PATH = path.join(__dirname, 'fixtures', 'spa');

const CONFIG: DeployConfig = {
  region: 'us-west-2',
  defaultModelId: 'anthropic.claude-3-5-sonnet-20241022-v2:0',
  routerModelId: 'anthropic.claude-3-haiku-20240307-v1:0',
  blockedModelIds: [],
  allowedEmailDomains: ['example.com'],
  adminEmail: 'admin@example.com',
  limits: DEFAULT_LIMITS,
  webSearchEnabled: true,
  accountDeletionEnabled: false,
  allowLocalhostCallback: false,
};

function attachmentsBucketId(template: Template): string {
  const ids = Object.keys(template.findResources('AWS::S3::Bucket')).filter((id) => id.startsWith('AttachmentsBucket'));
  expect(ids).toHaveLength(1);
  return ids[0];
}

describe.each([
  ['with deploy config (EdgeWafStack + BackendStack)', CONFIG],
  ['without deploy config (env-agnostic fallback)', undefined],
])('AttachmentsBucket wiring %s', (_label, config) => {
  process.env.CDK_DEFAULT_ACCOUNT = '123456789012';
  const app = new cdk.App();
  const built = buildStacks(app, config, FIXTURE_ASSET_PATH);
  // Synthesizing the whole app fails on any in-stack or cross-stack dependency cycle
  const assembly = app.synth();
  const template = Template.fromStack(built.backend);
  const bucketId = attachmentsBucketId(template);

  it('synthesizes every stack without a dependency cycle', () => {
    expect(assembly.stacks.length).toBe(config ? 2 : 1);
  });

  it('creates a private, encrypted, SSL-only, retained bucket with a generated name', () => {
    const bucket = template.findResources('AWS::S3::Bucket')[bucketId] as any;
    expect(bucket.DeletionPolicy).toBe('Retain');
    expect(bucket.Properties.BucketName).toBeUndefined();
    expect(bucket.Properties.PublicAccessBlockConfiguration).toEqual({
      BlockPublicAcls: true,
      BlockPublicPolicy: true,
      IgnorePublicAcls: true,
      RestrictPublicBuckets: true,
    });
    template.hasResourceProperties('AWS::S3::BucketPolicy', {
      Bucket: { Ref: bucketId },
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({ Effect: 'Deny', Condition: { Bool: { 'aws:SecureTransport': 'false' } } }),
        ]),
      },
    });
  });

  it('sets a POST-only CORS rule for the SPA origin through a custom resource (B32 presigned POST)', () => {
    const bucket = template.findResources('AWS::S3::Bucket')[bucketId] as any;
    expect(bucket.Properties.CorsConfiguration).toBeUndefined();
    const corsResources = Object.values(template.findResources('Custom::AWS')) as any[];
    const cors = corsResources.filter((r) => JSON.stringify(r.Properties.Create).includes('putBucketCors'));
    expect(cors).toHaveLength(1);
    const create = JSON.stringify(cors[0].Properties.Create);
    expect(create).toContain(bucketId);
    expect(create).toContain('\\"AllowedMethods\\":[\\"POST\\"]');
    expect(create).not.toContain('\\"AllowedOrigins\\":[\\"*\\"]');
    expect(create).toContain('DomainName');
    expect(create).not.toContain('localhost');
    expect(JSON.stringify(cors[0].Properties.Update)).toEqual(create);
  });

  it('gives the CORS custom resource a role limited to PutBucketCors on this bucket', () => {
    const policies = Object.values(template.findResources('AWS::IAM::Policy')) as any[];
    const corsPolicies = policies.filter((p) =>
      (p.Properties.Roles as Array<{ Ref: string }>).some((r) => r.Ref.startsWith('AttachmentsCorsRole')),
    );
    const statements = corsPolicies.flatMap((p) => p.Properties.PolicyDocument.Statement);
    const s3Statements = statements.filter((st: any) => JSON.stringify(st.Action).includes('s3:'));
    expect(s3Statements).toHaveLength(1);
    expect(s3Statements[0].Action).toBe('s3:PutBucketCors');
    expect(JSON.stringify(s3Statements[0].Resource)).toContain(bucketId);
    const roles = template.findResources('AWS::IAM::Role') as Record<string, any>;
    const corsRole = Object.entries(roles).find(([id]) => id.startsWith('AttachmentsCorsRole'))![1];
    expect(corsRole.Properties.ManagedPolicyArns).toBeUndefined();
  });

  it('passes the generated bucket name to every AgentCore runtime', () => {
    const runtimes = template.findResources('AWS::BedrockAgentCore::Runtime');
    expect(Object.keys(runtimes)).toHaveLength(4);
    for (const runtime of Object.values(runtimes)) {
      expect((runtime as any).Properties.EnvironmentVariables.CEI_ATTACHMENTS_BUCKET).toEqual({ Ref: bucketId });
    }
  });

  it('grants the runtime roles object read/write on that bucket only', () => {
    const policies = Object.values(template.findResources('AWS::IAM::Policy')) as any[];
    const runtimePolicies = policies.filter((p) =>
      (p.Properties.Roles as Array<{ Ref: string }>).some((r) => r.Ref.startsWith('AgentRuntimes')),
    );
    const objectGrants = runtimePolicies.filter((p) =>
      p.Properties.PolicyDocument.Statement.some(
        (s: any) =>
          JSON.stringify(s.Action) === JSON.stringify(['s3:GetObject', 's3:PutObject']) &&
          JSON.stringify(s.Resource).includes(bucketId),
      ),
    );
    expect(objectGrants).toHaveLength(4);
  });

  it('outputs the bucket name without a cross-stack export', () => {
    template.hasOutput('AttachmentsBucketName', { Value: { Ref: bucketId }, Export: Match.absent() });
  });
});
