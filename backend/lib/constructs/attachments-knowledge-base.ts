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
 * CUSTOM data source below (document id = attachment_id, inline metadata user_id/session_id/file_id/
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
    // Direct ingestion (IngestKnowledgeBaseDocuments) reads the source object by S3 URI through this role.
    role.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ['s3:GetObject'],
        resources: [props.attachmentsBucket.arnForObjects('uploads/*')],
      }),
    );
    NagSuppressions.addResourceSuppressions(
      role,
      [
        {
          id: 'AwsSolutions-IAM5',
          reason: 'ADR: Wildcard in S3 path for uploads prefix | Rationale: the KB service role reads whichever attachment object a direct-ingestion document points to under uploads/; object keys are per-user/session/attachment and created at runtime | Alternative: per-object policies rejected -- not practical for multi-tenant uploads',
        },
      ],
      true,
    );

    const knowledgeBase = new bedrock.CfnKnowledgeBase(this, 'KnowledgeBase', {
      // Stack-scoped name: two deployments in one account do not collide.
      name: `${stack.stackName}-attachments`,
      description: 'Managed knowledge base for chat attachment retrieval (B110)',
      roleArn: role.roleArn,
      knowledgeBaseConfiguration: {
        type: 'MANAGED',
        managedKnowledgeBaseConfiguration: { embeddingModelType: 'MANAGED' },
      },
    });
    // ADR: RETAIN | Rationale: same as the attachments bucket/table -- a stack teardown must not drop indexed user attachment content
    knowledgeBase.applyRemovalPolicy(RemovalPolicy.RETAIN);

    const dataSource = new bedrock.CfnDataSource(this, 'DataSource', {
      knowledgeBaseId: knowledgeBase.attrKnowledgeBaseId,
      name: 'AttachmentsCustomDataSource',
      // ADR: CUSTOM data source | Rationale: B110 ingests through IngestKnowledgeBaseDocuments (direct ingestion)
      // from the Attachment Processor Lambda, not a KB-managed crawl of a connector (S3/Confluence/SharePoint/etc.)
      // -- CUSTOM is the data source type with no connector configuration, matching the direct-ingestion contract
      dataSourceConfiguration: { type: 'CUSTOM' },
    });
    dataSource.applyRemovalPolicy(RemovalPolicy.RETAIN);

    this.knowledgeBaseId = knowledgeBase.attrKnowledgeBaseId;
    this.knowledgeBaseArn = knowledgeBase.attrKnowledgeBaseArn;
    this.dataSourceId = dataSource.attrDataSourceId;
  }
}
