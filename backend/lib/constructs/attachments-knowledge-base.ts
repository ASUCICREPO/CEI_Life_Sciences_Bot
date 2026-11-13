import * as cdk from 'aws-cdk-lib';
import { RemovalPolicy } from 'aws-cdk-lib';
import { aws_bedrock as bedrock } from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';

export interface AttachmentsKnowledgeBaseProps {
  /** Attachments bucket; the KB service role reads the source object under uploads/* by S3 URI during direct ingestion. */
  readonly attachmentsBucket: s3.IBucket;
}

/**
 * B110 (supersedes B33/B77): Amazon Bedrock Managed Knowledge Base for chat attachments. AWS runs the
 * vector store (`type: MANAGED`) -- there is no S3 Vectors index or OpenSearch collection to provision
 * here. The Attachment Processor Lambda pushes documents through `IngestKnowledgeBaseDocuments` on the
 * Custom connector data source below (document id = attachment_id, inline metadata user_id/session_id/file_id/
 * filename); this construct holds only the KB, its service role, and the data source -- not the
 * ingestion path or the retrieval call (that is runtime code per B110, not a Gateway target).
 */
export class AttachmentsKnowledgeBase extends Construct {
  public readonly knowledgeBaseId: string;
  public readonly knowledgeBaseArn: string;
  public readonly dataSourceId: string;

  constructor(scope: Construct, id: string, props: AttachmentsKnowledgeBaseProps) {
    super(scope, id);
    const stack = cdk.Stack.of(this);

    // ADR: KB service role trust scoped to this account's knowledge bases, not a fixed KB ARN | Rationale: the
    // KB id does not exist yet when CloudFormation creates the role, so AWS's own documented trust pattern
    // constrains the resource with aws:SourceAccount + an ArnLike on knowledge-base/* instead of naming one ARN
    const role = new iam.Role(this, 'KnowledgeBaseRole', {
      assumedBy: new iam.ServicePrincipal('bedrock.amazonaws.com').withConditions({
        StringEquals: { 'aws:SourceAccount': stack.account },
        ArnLike: { 'aws:SourceArn': `arn:${stack.partition}:bedrock:${stack.region}:${stack.account}:knowledge-base/*` },
      }),
    });
    // Direct ingestion (IngestKnowledgeBaseDocuments, S3_LOCATION) reads the source object through this role.
    // Same-account condition per kb-managed-permissions.html.
    role.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ['s3:GetObject'],
        resources: [props.attachmentsBucket.arnForObjects('uploads/*')],
        conditions: { StringEquals: { 'aws:ResourceAccount': stack.account } },
      }),
    );
    // The managed embedding model is bundled, so no bedrock:InvokeModel grant. The AWS managed-KB sample
    // (aws-samples/amazon-bedrock-samples, rag/managed-knowledge-bases/07-IaaC) grants the KB metrics namespace only.
    role.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ['cloudwatch:PutMetricData'],
        resources: ['*'],
        conditions: { StringEquals: { 'cloudwatch:namespace': 'AWS/Bedrock/KnowledgeBases' } },
      }),
    );
    NagSuppressions.addResourceSuppressions(
      role,
      [
        {
          id: 'AwsSolutions-IAM5',
          reason: 'ADR: Wildcard in S3 path for uploads prefix, and cloudwatch:PutMetricData on * | Rationale: the KB service role reads whichever attachment object a direct-ingestion document points to under uploads/ (object keys are per-user/session/attachment and created at runtime); PutMetricData has no resource-level permissions and is limited by the cloudwatch:namespace condition to AWS/Bedrock/KnowledgeBases | Alternative: per-object policies rejected -- not practical for multi-tenant uploads',
        },
      ],
      true,
    );

    const knowledgeBase = new bedrock.CfnKnowledgeBase(this, 'KnowledgeBase', {
      // Stack-scoped name: two deployments in one account do not collide.
      name: `${stack.stackName}-attachments-kb`,
      description: 'Managed knowledge base for chat attachment retrieval (B110)',
      roleArn: role.roleArn,
      knowledgeBaseConfiguration: {
        type: 'MANAGED',
        managedKnowledgeBaseConfiguration: { embeddingModelType: 'MANAGED' },
      },
    });
    // ADR: RETAIN_ON_UPDATE_OR_DELETE | Rationale: a stack teardown or replacement must not drop indexed user attachment
    // content, but a failed first create must roll back cleanly (plain RETAIN left an orphan KB on 2026-11-11) |
    // Alternative: RETAIN rejected (orphans on create rollback); DESTROY rejected (loses user data on teardown)
    knowledgeBase.applyRemovalPolicy(RemovalPolicy.RETAIN_ON_UPDATE_OR_DELETE);

    const dataSource = new bedrock.CfnDataSource(this, 'DataSource', {
      knowledgeBaseId: knowledgeBase.attrKnowledgeBaseId,
      name: 'AttachmentsCustomDataSource',
      // ADR: Custom connector on a managed KB | Rationale: B110 ingests through IngestKnowledgeBaseDocuments from the
      // Attachment Processor; a MANAGED KB accepts only type MANAGED_KNOWLEDGE_BASE_CONNECTOR, with the connector kind
      // in connectorParameters (kb-managed-ds-custom.html). A top-level type CUSTOM is the self-managed form and
      // CloudFormation rejects it ("Unsupported data source type for MANAGED knowledge base type") | Alternative: S3
      // connector rejected -- its scheduled sync can overwrite or remove a document a user just added
      dataSourceConfiguration: {
        type: 'MANAGED_KNOWLEDGE_BASE_CONNECTOR',
        managedKnowledgeBaseConnectorConfiguration: {
          connectorParameters: { type: 'CUSTOM', version: '1', aclEnabled: false },
        },
      },
    });
    dataSource.applyRemovalPolicy(RemovalPolicy.RETAIN_ON_UPDATE_OR_DELETE);

    this.knowledgeBaseId = knowledgeBase.attrKnowledgeBaseId;
    this.knowledgeBaseArn = knowledgeBase.attrKnowledgeBaseArn;
    this.dataSourceId = dataSource.attrDataSourceId;
  }
}
