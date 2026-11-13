import * as iam from 'aws-cdk-lib/aws-iam';
import { NagSuppressions } from 'cdk-nag';

/** X-Ray's ingestion actions do not support resource-level IAM permissions. */
export function suppressXrayWildcard(role: iam.Role): void {
  NagSuppressions.addResourceSuppressions(role, [{
    id: 'AwsSolutions-IAM5',
    appliesTo: ['Resource::*'],
    reason: 'ADR: X-Ray ingestion wildcard | Rationale: PutTraceSegments and PutTelemetryRecords do not support resource-level permissions | Alternative: resource-scoped tracing rejected because X-Ray does not support it',
  }], true);
}
