import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Annotations, Match, Template } from 'aws-cdk-lib/assertions';
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

function build(): { stack: cdk.Stack; template: Template } {
  process.env.CDK_DEFAULT_ACCOUNT = '123456789012';
  const app = new cdk.App();
  const built = buildStacks(app, CONFIG, FIXTURE_ASSET_PATH);
  app.synth();
  return { stack: built.backend, template: Template.fromStack(built.backend) };
}

describe('B110: Managed Knowledge Base for attachments', () => {
  const { stack, template } = build();

  it('creates exactly one Managed knowledge base (no S3 Vectors/OpenSearch storage configuration)', () => {
    const kbs = template.findResources('AWS::Bedrock::KnowledgeBase');
    expect(Object.keys(kbs)).toHaveLength(1);
    const kb = Object.values(kbs)[0] as any;
    expect(kb.Properties.KnowledgeBaseConfiguration.Type).toBe('MANAGED');
    expect(kb.Properties.KnowledgeBaseConfiguration.ManagedKnowledgeBaseConfiguration).toEqual({
      EmbeddingModelType: 'MANAGED',
    });
    expect(kb.Properties.StorageConfiguration).toBeUndefined();
    // Kept on delete and replacement, but removed when a failed first create rolls back (no orphan KB).
    expect(kb.DeletionPolicy).toBe('RetainExceptOnCreate');
    expect(kb.UpdateReplacePolicy).toBe('Retain');
  });

  it('creates exactly one Custom connector data source on that knowledge base', () => {
    const sources = template.findResources('AWS::Bedrock::DataSource');
    expect(Object.keys(sources)).toHaveLength(1);
    const source = Object.values(sources)[0] as any;
    // A MANAGED KB needs the managed connector type; a top-level CUSTOM fails at deploy time.
    expect(source.Properties.DataSourceConfiguration.Type).toBe('MANAGED_KNOWLEDGE_BASE_CONNECTOR');
    expect(source.Properties.DataSourceConfiguration.ManagedKnowledgeBaseConnectorConfiguration.ConnectorParameters)
      .toEqual({ type: 'CUSTOM', version: '1', aclEnabled: false });
  });

  it('removes the KBChunksBucket -- exactly four private buckets remain (site, logs, attachments, exports)', () => {
    const bucketIds = Object.keys(template.findResources('AWS::S3::Bucket'));
    expect(bucketIds.some((id) => id.startsWith('KBChunksBucket'))).toBe(false);
  });

  it('grants the KB service role s3:GetObject on uploads/* only, trusted by bedrock.amazonaws.com scoped to this account', () => {
    const roles = template.findResources('AWS::IAM::Role');
    const kbRoleId = Object.keys(roles).find((id) => id.startsWith('AttachmentsKnowledgeBaseKnowledgeBaseRole'));
    expect(kbRoleId).toBeDefined();
    const kbRole = roles[kbRoleId!] as any;
    const trustStatement = kbRole.Properties.AssumeRolePolicyDocument.Statement[0];
    expect(trustStatement.Principal.Service).toBe('bedrock.amazonaws.com');
    expect(trustStatement.Condition.StringEquals['aws:SourceAccount']).toBeDefined();
    expect(JSON.stringify(trustStatement.Condition.ArnLike['aws:SourceArn'])).toContain('knowledge-base/*');

    const policies = Object.values(template.findResources('AWS::IAM::Policy')) as any[];
    const kbPolicy = policies.find((p) =>
      (p.Properties.Roles as Array<{ Ref: string }>).some((r) => r.Ref === kbRoleId),
    );
    expect(kbPolicy).toBeDefined();
    const s3Statement = kbPolicy.Properties.PolicyDocument.Statement.find((s: any) =>
      [s.Action].flat().includes('s3:GetObject'),
    );
    expect(s3Statement).toBeDefined();
    expect(JSON.stringify(s3Statement.Resource)).toContain('uploads/*');
  });

  it('wires the S3 upload notification to SQS, not directly to the processor Lambda', () => {
    const buckets = template.findResources('AWS::S3::Bucket');
    const attachmentsBucketId = Object.keys(buckets).find((id) => id.startsWith('AttachmentsBucket'))!;

    const customResources = template.findResources('Custom::S3BucketNotifications');
    const notification = Object.values(customResources).find(
      (r: any) => r.Properties.BucketName.Ref === attachmentsBucketId,
    ) as any;
    expect(notification).toBeDefined();
    const configs = JSON.stringify(notification.Properties.NotificationConfiguration);
    expect(configs).not.toContain('LambdaFunctionConfigurations');
    expect(configs).toContain('QueueConfigurations');
  });

  it('processor Lambda has an SQS event source and the exact B110 env var names', () => {
    const fns = template.findResources('AWS::Lambda::Function');
    const processorId = Object.keys(fns).find((id) => id.startsWith('AttachmentProcessorFn'))!;
    const processor = fns[processorId] as any;
    const env = processor.Properties.Environment.Variables;
    expect(Object.keys(env).sort()).toEqual(
      ['ATTACHMENTS_TABLE', 'ATTACHMENTS_BUCKET', 'KNOWLEDGE_BASE_ID', 'KNOWLEDGE_BASE_DATA_SOURCE_ID'].sort(),
    );
    expect(env.KNOWLEDGE_BASE_ID).toBeDefined();
    expect(env.KNOWLEDGE_BASE_DATA_SOURCE_ID).toBeDefined();

    const eventSources = template.findResources('AWS::Lambda::EventSourceMapping');
    const mapping = Object.values(eventSources).find(
      (m: any) => m.Properties.FunctionName.Ref === processorId,
    ) as any;
    expect(mapping).toBeDefined();
    expect(mapping.Properties.BatchSize).toBe(1);
    expect(mapping.Properties.FunctionResponseTypes).toEqual(['ReportBatchItemFailures']);
  });

  it('processor role gets Ingest/Get KnowledgeBaseDocuments scoped to the KB ARN, plus GetItem/UpdateItem on attachments', () => {
    const policies = Object.values(template.findResources('AWS::IAM::Policy')) as any[];
    const processorPolicies = policies.filter((p) =>
      (p.Properties.Roles as Array<{ Ref: string }>).some((r) => r.Ref.startsWith('AttachmentProcessorRole')),
    );
    const statements = processorPolicies.flatMap((p) => p.Properties.PolicyDocument.Statement);

    const bedrockStatement = statements.find((s: any) => [s.Action].flat().includes('bedrock:IngestKnowledgeBaseDocuments'));
    expect(bedrockStatement).toBeDefined();
    expect([bedrockStatement.Action].flat()).toEqual(
      expect.arrayContaining(['bedrock:IngestKnowledgeBaseDocuments', 'bedrock:GetKnowledgeBaseDocuments']),
    );
    expect(JSON.stringify(bedrockStatement.Resource)).not.toContain('"*"');

    const dynamoStatement = statements.find((s: any) =>
      [s.Action].flat().includes('dynamodb:GetItem') && [s.Action].flat().includes('dynamodb:UpdateItem'),
    );
    expect(dynamoStatement).toBeDefined();
  });

  it('creates an attachment-processing queue with a DLQ (maxReceiveCount 3) and a visibility timeout >= 6x the Lambda timeout', () => {
    const queues = template.findResources('AWS::SQS::Queue');
    const queueId = Object.keys(queues).find((id) => id.startsWith('AttachmentProcessorQueue'))!;
    const dlqId = Object.keys(queues).find((id) => id.startsWith('AttachmentProcessorDLQ'))!;
    expect(queueId).toBeDefined();
    expect(dlqId).toBeDefined();
    const queue = queues[queueId] as any;
    expect(queue.Properties.VisibilityTimeout).toBeGreaterThanOrEqual(5 * 60 * 6);
    expect(queue.Properties.RedrivePolicy.maxReceiveCount).toBe(3);
    expect(queue.Properties.RedrivePolicy.deadLetterTargetArn['Fn::GetAtt'][0]).toBe(dlqId);
    for (const id of [queueId, dlqId]) {
      template.hasResourceProperties('AWS::SQS::Queue', Match.anyValue());
      expect((queues[id] as any).Properties.SqsManagedSseEnabled).toBe(true);
    }
  });

  it('every AgentCore runtime gets CEI_KNOWLEDGE_BASE_ID and a bedrock:Retrieve grant scoped to the KB ARN', () => {
    const runtimes = template.findResources('AWS::BedrockAgentCore::Runtime');
    expect(Object.keys(runtimes)).toHaveLength(5);
    for (const runtime of Object.values(runtimes)) {
      expect((runtime as any).Properties.EnvironmentVariables.CEI_KNOWLEDGE_BASE_ID).toBeDefined();
    }
    const policies = Object.values(template.findResources('AWS::IAM::Policy')) as any[];
    const runtimeRetrieveGrants = policies.filter((p) =>
      (p.Properties.Roles as Array<{ Ref: string }>).some((r) => r.Ref.startsWith('AgentRuntimes')) &&
      p.Properties.PolicyDocument.Statement.some((s: any) => [s.Action].flat().includes('bedrock:Retrieve')),
    );
    expect(runtimeRetrieveGrants).toHaveLength(5);
    for (const p of runtimeRetrieveGrants) {
      const statement = p.Properties.PolicyDocument.Statement.find((s: any) => [s.Action].flat().includes('bedrock:Retrieve'));
      expect(JSON.stringify(statement.Resource)).not.toContain('"*"');
    }
  });

  it('produces zero unsuppressed cdk-nag AwsSolutions errors', () => {
    const errors = Annotations.fromStack(stack).findError('*', Match.stringLikeRegexp('AwsSolutions-.*'));
    expect(errors).toHaveLength(0);
  });
});
