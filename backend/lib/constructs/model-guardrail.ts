import { aws_bedrock as bedrock } from 'aws-cdk-lib';
import { Construct } from 'constructs';

/**
 * F8 input PII Guardrail foundation. This construct is deliberately not added to
 * BackendStack until request tagging/pre-masking and runtime IAM are integrated.
 *
 * B51a limits PII masking to user input. This conservative starting subset is an
 * implementation default, not a complete PHI policy: B51's entity list remains
 * open for review and must be validated in the development deployment.
 */
export const DEFAULT_INPUT_PII_TYPES = [
  'NAME', 'ADDRESS', 'EMAIL', 'PHONE', 'US_SOCIAL_SECURITY_NUMBER',
] as const;

export class ModelGuardrail extends Construct {
  readonly guardrailId: string;
  readonly guardrailArn: string;
  readonly guardrailVersion: string;

  constructor(scope: Construct, id: string) {
    super(scope, id);
    const guardrail = new bedrock.CfnGuardrail(this, 'Resource', {
      name: 'cei-input-pii',
      description: 'Masks selected PII in user-authored input; output PII filtering is disabled.',
      blockedInputMessaging: 'This request could not be processed.',
      blockedOutputsMessaging: 'This response could not be processed.',
      sensitiveInformationPolicyConfig: {
        piiEntitiesConfig: DEFAULT_INPUT_PII_TYPES.map((type) => ({
          type,
          // Action is required by CloudFormation; per-direction flags explicitly
          // disable output evaluation and prevent output masking.
          action: 'ANONYMIZE',
          inputAction: 'ANONYMIZE',
          inputEnabled: true,
          outputAction: 'NONE',
          outputEnabled: false,
        })),
      },
    });
    const version = new bedrock.CfnGuardrailVersion(this, 'Version', {
      guardrailIdentifier: guardrail.attrGuardrailId,
      description: 'Initial input-only PII policy.',
    });
    this.guardrailId = guardrail.attrGuardrailId;
    this.guardrailArn = guardrail.attrGuardrailArn;
    this.guardrailVersion = version.attrVersion;
  }
}
