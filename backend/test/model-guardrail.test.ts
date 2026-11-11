import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { DEFAULT_INPUT_PII_TYPES, ModelGuardrail } from '../lib/constructs/model-guardrail';

describe('F8 input-only PII guardrail', () => {
  test('creates a versioned guardrail with input masking and output disabled', () => {
    const stack = new cdk.Stack(new cdk.App(), 'ModelGuardrailTest');
    const guardrail = new ModelGuardrail(stack, 'InputGuardrail');
    const template = Template.fromStack(stack);
    const entities = DEFAULT_INPUT_PII_TYPES.map((type) => ({
      Type: type, Action: 'ANONYMIZE', InputAction: 'ANONYMIZE', InputEnabled: true,
      OutputAction: 'NONE', OutputEnabled: false,
    }));
    template.resourceCountIs('AWS::Bedrock::Guardrail', 1);
    template.hasResourceProperties('AWS::Bedrock::Guardrail', {
      SensitiveInformationPolicyConfig: { PiiEntitiesConfig: entities },
    });
    template.resourceCountIs('AWS::Bedrock::GuardrailVersion', 1);
    expect(cdk.Token.isUnresolved(guardrail.guardrailId)).toBe(true);
    expect(cdk.Token.isUnresolved(guardrail.guardrailArn)).toBe(true);
    expect(cdk.Token.isUnresolved(guardrail.guardrailVersion)).toBe(true);
  });
});
