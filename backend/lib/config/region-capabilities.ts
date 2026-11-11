/**
 * Pure Region-capability feature matrix and evaluation logic. No AWS calls here —
 * `scripts/preflight.ts` gathers the raw SSM/Bedrock facts and passes them in.
 */

const AGENTCORE_REGIONS_DOC_URL =
  'https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-regions.html';
const S3_VECTORS_DOC_URL =
  'https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-regions-quotas.html';
const KB_DOC_URL = 'https://docs.aws.amazon.com/bedrock/latest/userguide/knowledge-base-setup.html';
const WEB_SEARCH_DOC_URL =
  'https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-target-connector-web-search-tool.html';

// ADR: hardcoded Region list | Rationale: AgentCore has no reliable per-feature SSM probe; doc-checked list plus SSM coarse gate | Alternative: SSM-only (rejected, cannot detect feature-level gaps like Memory)
export const AGENTCORE_CORE_REGIONS: readonly string[] = [
  'us-east-1',
  'us-east-2',
  'us-west-1',
  'us-west-2',
  'eu-central-1',
  'eu-west-1',
  'eu-west-2',
  'eu-south-1',
  'eu-west-3',
  'eu-south-2',
  'eu-north-1',
  'ap-southeast-5',
  'ap-south-1',
  'ap-south-2',
  'ap-southeast-1',
  'ap-southeast-2',
  'ap-southeast-7',
  'ap-northeast-1',
  'ap-northeast-2',
  'ca-central-1',
  'sa-east-1',
  'us-gov-west-1',
];

const MEMORY_EXCLUDED_REGIONS: readonly string[] = [
  'us-west-1',
  'eu-south-1',
  'eu-south-2',
  'ap-southeast-5',
  'ap-south-2',
  'ap-southeast-7',
];

export const AGENTCORE_MEMORY_REGIONS: readonly string[] = AGENTCORE_CORE_REGIONS.filter(
  (region) => !MEMORY_EXCLUDED_REGIONS.includes(region),
);

export const WEB_SEARCH_REGIONS: readonly string[] = ['us-east-1', 'eu-west-1', 'ap-northeast-1'];

interface RegionListFeature {
  kind: 'region-list';
  regions: readonly string[];
  docUrl: string;
  checkedOn: string;
}

interface RuleFeature {
  kind: 'rule';
  rule: string;
  docUrl: string;
  checkedOn: string;
}

/** Per-feature capability facts, each with the doc URL and date they were last checked against AWS docs. */
export const FEATURE_MATRIX: Record<string, RegionListFeature | RuleFeature> = {
  agentCoreRuntime: {
    kind: 'region-list',
    regions: AGENTCORE_CORE_REGIONS,
    docUrl: AGENTCORE_REGIONS_DOC_URL,
    checkedOn: '2026-09-29',
  },
  agentCoreGateway: {
    kind: 'region-list',
    regions: AGENTCORE_CORE_REGIONS,
    docUrl: AGENTCORE_REGIONS_DOC_URL,
    checkedOn: '2026-09-29',
  },
  agentCorePolicy: {
    kind: 'region-list',
    regions: AGENTCORE_CORE_REGIONS,
    docUrl: AGENTCORE_REGIONS_DOC_URL,
    checkedOn: '2026-09-29',
  },
  agentCoreBuiltInTools: {
    kind: 'region-list',
    regions: AGENTCORE_CORE_REGIONS,
    docUrl: AGENTCORE_REGIONS_DOC_URL,
    checkedOn: '2026-09-29',
  },
  agentCoreMemory: {
    kind: 'region-list',
    regions: AGENTCORE_MEMORY_REGIONS,
    docUrl: AGENTCORE_REGIONS_DOC_URL,
    checkedOn: '2026-09-29',
  },
  s3Vectors: {
    kind: 'rule',
    rule: 'SSM global-infrastructure service id "s3vectors" exists in the Region',
    docUrl: S3_VECTORS_DOC_URL,
    checkedOn: '2026-09-29',
  },
  bedrockKnowledgeBases: {
    kind: 'rule',
    // B110: Managed Knowledge Base (type: MANAGED) -- AWS runs the vector store, so no S3 Vectors capability is needed.
    rule: 'SSM global-infrastructure service id "bedrock" exists in the Region',
    docUrl: KB_DOC_URL,
    checkedOn: '2026-11-11',
  },
  webSearch: {
    kind: 'region-list',
    regions: WEB_SEARCH_REGIONS,
    docUrl: WEB_SEARCH_DOC_URL,
    checkedOn: '2026-09-29',
  },
};

/** A model is an inference profile (not a plain foundation model) if its ID has a geo prefix. */
const INFERENCE_PROFILE_PREFIX = /^(us|eu|apac|global)\./;

export interface ModelAvailability {
  regionAvailability: string;
  authorizationStatus: string;
  entitlementAvailability: string;
  agreementStatus: string;
}

export interface ModelCheckInput {
  modelId: string;
  role: 'default' | 'router';
  availability?: ModelAvailability;
  inferenceProfileStatus?: string;
  error?: string;
}

export interface SsmServiceProbe {
  'bedrock-agentcore': boolean;
  s3vectors: boolean;
  bedrock: boolean;
}

export interface EvaluateRegionInput {
  region: string;
  ssmServices: SsmServiceProbe;
  models: ModelCheckInput[];
}

export interface EvaluateRegionResult {
  errors: string[];
  warnings: string[];
  webSearchEnabled: boolean;
}

function evaluateAgentCoreCore(input: EvaluateRegionInput, errors: string[]): boolean {
  const ssmOk = input.ssmServices['bedrock-agentcore'];
  const docOk = AGENTCORE_CORE_REGIONS.includes(input.region);
  if (ssmOk && docOk) {
    return true;
  }
  errors.push(
    `AgentCore Runtime, Gateway, Policy, and Built-in tools are not available in ${input.region} ` +
      `(${ssmOk ? 'not in the documented AgentCore Region list' : 'SSM service "bedrock-agentcore" not found for this Region'}). ` +
      `See ${FEATURE_MATRIX.agentCoreRuntime.docUrl} (checked ${FEATURE_MATRIX.agentCoreRuntime.checkedOn}).`,
  );
  return false;
}

function evaluateAgentCoreMemory(input: EvaluateRegionInput, errors: string[], agentCoreCoreOk: boolean): void {
  if (!agentCoreCoreOk) {
    // Core AgentCore is already reported as missing; do not double-report Memory.
    return;
  }
  if (AGENTCORE_MEMORY_REGIONS.includes(input.region)) {
    return;
  }
  errors.push(
    `AgentCore Memory is not available in ${input.region}. ` +
      `See ${FEATURE_MATRIX.agentCoreMemory.docUrl} (checked ${FEATURE_MATRIX.agentCoreMemory.checkedOn}).`,
  );
}

function evaluateS3Vectors(input: EvaluateRegionInput, errors: string[]): boolean {
  if (input.ssmServices.s3vectors) {
    return true;
  }
  errors.push(
    `S3 Vectors is not available in ${input.region} (SSM service "s3vectors" not found for this Region). ` +
      `See ${FEATURE_MATRIX.s3Vectors.docUrl} (checked ${FEATURE_MATRIX.s3Vectors.checkedOn}).`,
  );
  return false;
}

function evaluateBedrockKnowledgeBases(input: EvaluateRegionInput, errors: string[]): void {
  // B110: the attachments KB is Managed (AWS-run vector store), so only the "bedrock" service id matters here.
  if (input.ssmServices.bedrock) {
    return;
  }
  errors.push(
    `Bedrock Knowledge Bases are not available in ${input.region} ` +
      `(SSM service "bedrock" not found for this Region). ` +
      `See ${FEATURE_MATRIX.bedrockKnowledgeBases.docUrl} (checked ${FEATURE_MATRIX.bedrockKnowledgeBases.checkedOn}).`,
  );
}

function evaluateWebSearch(input: EvaluateRegionInput, warnings: string[]): boolean {
  if (WEB_SEARCH_REGIONS.includes(input.region)) {
    return true;
  }
  warnings.push(
    `AgentCore Web Search is not available in ${input.region}; webSearchEnabled will be set to false. ` +
      `See ${FEATURE_MATRIX.webSearch.docUrl} (checked ${FEATURE_MATRIX.webSearch.checkedOn}).`,
  );
  return false;
}

function evaluateModel(model: ModelCheckInput, region: string, errors: string[]): void {
  const label = `Model "${model.modelId}" (${model.role})`;

  if (model.error) {
    errors.push(`${label}: could not check availability in ${region}: ${model.error}`);
    return;
  }

  if (!model.availability) {
    errors.push(`${label}: no availability information was provided for ${region}`);
    return;
  }

  const { regionAvailability, authorizationStatus, entitlementAvailability, agreementStatus } =
    model.availability;

  if (regionAvailability !== 'AVAILABLE') {
    errors.push(`${label} is not available in Region ${region} (regionAvailability=${regionAvailability}).`);
  }
  if (authorizationStatus !== 'AUTHORIZED') {
    errors.push(`${label} is not authorized in Region ${region} (authorizationStatus=${authorizationStatus}).`);
  }
  if (entitlementAvailability !== 'AVAILABLE') {
    errors.push(
      `${label} has no entitlement available in Region ${region} (entitlementAvailability=${entitlementAvailability}).`,
    );
  }
  if (agreementStatus === 'PENDING') {
    errors.push(`${label}: the Marketplace agreement is still PENDING in Region ${region}.`);
  } else if (agreementStatus !== 'AVAILABLE') {
    errors.push(`${label}: the Marketplace agreement is not available in Region ${region} (agreementStatus=${agreementStatus}).`);
  }

  if (INFERENCE_PROFILE_PREFIX.test(model.modelId)) {
    if (model.inferenceProfileStatus !== 'ACTIVE') {
      errors.push(
        `${label} is an inference profile that is not ACTIVE in Region ${region} ` +
          `(inferenceProfileStatus=${model.inferenceProfileStatus ?? 'UNKNOWN'}).`,
      );
    }
  }
}

/**
 * Evaluate whether a Region has every AgentCore capability the target architecture needs,
 * and whether the configured models are usable there. Pure — no AWS calls.
 */
export function evaluateRegion(input: EvaluateRegionInput): EvaluateRegionResult {
  const errors: string[] = [];
  const warnings: string[] = [];

  const agentCoreCoreOk = evaluateAgentCoreCore(input, errors);
  evaluateAgentCoreMemory(input, errors, agentCoreCoreOk);
  evaluateS3Vectors(input, errors);
  evaluateBedrockKnowledgeBases(input, errors);
  const webSearchEnabled = evaluateWebSearch(input, warnings);

  for (const model of input.models) {
    evaluateModel(model, input.region, errors);
  }

  return { errors, warnings, webSearchEnabled };
}
