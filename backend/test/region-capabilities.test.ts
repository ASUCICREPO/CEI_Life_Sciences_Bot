import { evaluateRegion, EvaluateRegionInput, ModelCheckInput } from '../lib/config/region-capabilities';

function okSsm() {
  return { 'bedrock-agentcore': true, s3vectors: true, bedrock: true };
}

function okModel(overrides: Partial<ModelCheckInput> = {}): ModelCheckInput {
  return {
    modelId: 'anthropic.claude-3-5-sonnet-20241022-v2:0',
    role: 'default',
    availability: {
      regionAvailability: 'AVAILABLE',
      authorizationStatus: 'AUTHORIZED',
      entitlementAvailability: 'AVAILABLE',
      agreementStatus: 'AVAILABLE',
    },
    ...overrides,
  };
}

describe('evaluateRegion', () => {
  it('us-east-1: everything available, web search on', () => {
    const input: EvaluateRegionInput = {
      region: 'us-east-1',
      ssmServices: okSsm(),
      models: [okModel({ role: 'default' }), okModel({ role: 'router' })],
    };
    const result = evaluateRegion(input);
    expect(result.errors).toEqual([]);
    expect(result.webSearchEnabled).toBe(true);
  });

  it('us-east-2: everything available, web search off (warning, not error)', () => {
    const input: EvaluateRegionInput = {
      region: 'us-east-2',
      ssmServices: okSsm(),
      models: [okModel({ role: 'default' }), okModel({ role: 'router' })],
    };
    const result = evaluateRegion(input);
    expect(result.errors).toEqual([]);
    expect(result.webSearchEnabled).toBe(false);
    expect(result.warnings.some((w) => w.includes('Web Search'))).toBe(true);
  });

  it('us-west-1: AgentCore Memory error, core AgentCore still ok', () => {
    const input: EvaluateRegionInput = {
      region: 'us-west-1',
      ssmServices: okSsm(),
      models: [okModel({ role: 'default' }), okModel({ role: 'router' })],
    };
    const result = evaluateRegion(input);
    expect(result.errors).toHaveLength(1);
    expect(result.errors[0]).toContain('AgentCore Memory');
    expect(result.errors[0]).toContain('us-west-1');
  });

  it('ap-southeast-3 with SSM bedrock-agentcore=false: AgentCore missing, no duplicate Memory error', () => {
    const input: EvaluateRegionInput = {
      region: 'ap-southeast-3',
      ssmServices: { 'bedrock-agentcore': false, s3vectors: true, bedrock: true },
      models: [okModel({ role: 'default' }), okModel({ role: 'router' })],
    };
    const result = evaluateRegion(input);
    const agentCoreErrors = result.errors.filter((e) => e.includes('AgentCore'));
    expect(agentCoreErrors).toHaveLength(1);
    expect(agentCoreErrors[0]).toContain('ap-southeast-3');
    expect(result.errors.some((e) => e.includes('AgentCore Memory'))).toBe(false);
  });

  it('reports an unauthorized model', () => {
    const input: EvaluateRegionInput = {
      region: 'us-east-1',
      ssmServices: okSsm(),
      models: [
        okModel({
          availability: {
            regionAvailability: 'AVAILABLE',
            authorizationStatus: 'NOT_AUTHORIZED',
            entitlementAvailability: 'AVAILABLE',
            agreementStatus: 'AVAILABLE',
          },
        }),
        okModel({ role: 'router' }),
      ],
    };
    const result = evaluateRegion(input);
    expect(
      result.errors.some((e) => e.includes('not authorized') && e.includes('anthropic.claude-3-5-sonnet-20241022-v2:0')),
    ).toBe(true);
  });

  it('reports a pending Marketplace agreement', () => {
    const input: EvaluateRegionInput = {
      region: 'us-east-1',
      ssmServices: okSsm(),
      models: [
        okModel({
          availability: {
            regionAvailability: 'AVAILABLE',
            authorizationStatus: 'AUTHORIZED',
            entitlementAvailability: 'AVAILABLE',
            agreementStatus: 'PENDING',
          },
        }),
        okModel({ role: 'router' }),
      ],
    };
    const result = evaluateRegion(input);
    expect(result.errors.some((e) => e.includes('PENDING'))).toBe(true);
  });

  it('reports an inactive inference profile', () => {
    const input: EvaluateRegionInput = {
      region: 'us-east-1',
      ssmServices: okSsm(),
      models: [
        okModel({ modelId: 'us.anthropic.claude-3-5-sonnet-20241022-v2:0', inferenceProfileStatus: 'CREATING' }),
        okModel({ role: 'router' }),
      ],
    };
    const result = evaluateRegion(input);
    expect(result.errors.some((e) => e.includes('inference profile') && e.includes('CREATING'))).toBe(true);
  });

  it('reports a model-check error passthrough', () => {
    const input: EvaluateRegionInput = {
      region: 'us-east-1',
      ssmServices: okSsm(),
      models: [{ modelId: 'bad.model', role: 'default', error: 'AccessDeniedException' }, okModel({ role: 'router' })],
    };
    const result = evaluateRegion(input);
    expect(result.errors.some((e) => e.includes('bad.model') && e.includes('AccessDeniedException'))).toBe(true);
  });
});
