import * as fs from 'fs';
import * as path from 'path';
import type * as cdk from 'aws-cdk-lib';

/**
 * Per-user usage limits (B22).
 */
export interface DeployLimits {
  requestsPerSecond: number;
  maxRealtimeRunsPerUser: number;
  maxBackgroundRunsPerUser: number;
}

/** Cedar policy engine mode on the tool Gateway (B30: start LOG_ONLY, then ENFORCE). */
export type ToolPolicyMode = 'LOG_ONLY' | 'ENFORCE';
export const TOOL_POLICY_MODES: readonly ToolPolicyMode[] = ['LOG_ONLY', 'ENFORCE'];
export const DEFAULT_TOOL_POLICY_MODE: ToolPolicyMode = 'LOG_ONLY';

/**
 * The configuration `deploy.sh` collects and writes to a CDK context JSON file,
 * and that the CDK app / stack read back at synth/deploy time.
 */
export interface DeployConfig {
  region: string;
  defaultModelId: string;
  routerModelId: string;
  blockedModelIds: string[];
  allowedEmailDomains: string[];
  adminEmail: string;
  limits: DeployLimits;
  webSearchEnabled: boolean;
  /** B90: adds the localhost:5173 callback/logout URLs to the SPA Cognito client for local dev. Default false. */
  allowLocalhostCallback: boolean;
  /** F4/B30: Cedar policy mode on the tool Gateway. Starts LOG_ONLY, flipped to ENFORCE after the audit. Default LOG_ONLY. */
  toolPolicyMode?: ToolPolicyMode;
  repository?: {
    url: string;
    branch: string;
  };
}

// ADR: B22 defaults | Rationale: decision log fixes 20 rps / 2 real-time / 1 background as the MVP limits
export const DEFAULT_LIMITS: DeployLimits = {
  requestsPerSecond: 20,
  maxRealtimeRunsPerUser: 2,
  maxBackgroundRunsPerUser: 1,
};

const DEFAULT_CONFIG_FILE_NAME = 'cei.deploy.json';
const CONTEXT_KEY = 'ceiConfigFile';

const REGION_PATTERN = /^[a-z]{2}(-gov)?-[a-z]+-\d$/;
const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const DOMAIN_PATTERN = /^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$/i;
const MODEL_ID_PATTERN = /^[A-Za-z0-9._:/-]{1,200}$/;
// ADR: GitHub-only, HTTPS-only repository URL | Rationale: block ssh/other hosts and embedded credentials (security review) | Alternative: generic URL parsing (rejected — cannot enforce "no credentials")
const REPOSITORY_URL_PATTERN = /^https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+(\.git)?$/;
const REPOSITORY_BRANCH_PATTERN = /^[A-Za-z0-9._/-]{1,255}$/;

export class DeployConfigError extends Error {
  public readonly errors: string[];

  constructor(errors: string[]) {
    super(`Invalid deploy config:\n${errors.map((e) => `  - ${e}`).join('\n')}`);
    this.name = 'DeployConfigError';
    this.errors = errors;
  }
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isPositiveInteger(value: unknown): value is number {
  return typeof value === 'number' && Number.isInteger(value) && value > 0;
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === 'string' && value.trim().length > 0;
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((v) => typeof v === 'string');
}

function parseLimits(raw: unknown, errors: string[]): DeployLimits {
  if (raw === undefined) {
    return { ...DEFAULT_LIMITS };
  }
  if (!isPlainObject(raw)) {
    errors.push('limits: must be an object');
    return { ...DEFAULT_LIMITS };
  }

  const limits: DeployLimits = { ...DEFAULT_LIMITS };

  const fields: Array<keyof DeployLimits> = [
    'requestsPerSecond',
    'maxRealtimeRunsPerUser',
    'maxBackgroundRunsPerUser',
  ];
  for (const field of fields) {
    const value = raw[field];
    if (value === undefined) {
      continue;
    }
    if (!isPositiveInteger(value)) {
      errors.push(`limits.${field}: must be a positive integer`);
      continue;
    }
    limits[field] = value;
  }

  return limits;
}

function parseRepository(
  raw: unknown,
  errors: string[],
): { url: string; branch: string } | undefined {
  if (raw === undefined) {
    return undefined;
  }
  if (!isPlainObject(raw)) {
    errors.push('repository: must be an object with "url" and "branch"');
    return undefined;
  }

  // ADR: never echo the rejected repository URL | Rationale: a private-repo URL may carry a PAT (e.g. https://user:ghp_xxx@github.com/...); logging it would leak the credential
  let url: string | undefined;
  if (isNonEmptyString(raw.url) && REPOSITORY_URL_PATTERN.test(raw.url)) {
    url = raw.url;
  } else {
    errors.push('repository.url: must be https://github.com/<owner>/<repo>[.git] with no credentials');
  }

  let branch: string | undefined;
  if (
    isNonEmptyString(raw.branch) &&
    REPOSITORY_BRANCH_PATTERN.test(raw.branch) &&
    !raw.branch.includes('..')
  ) {
    branch = raw.branch;
  } else {
    errors.push('repository.branch: must match ^[A-Za-z0-9._/-]{1,255}$ and must not contain ".."');
  }

  if (url !== undefined && branch !== undefined) {
    return { url, branch };
  }
  return undefined;
}

/**
 * Validate and normalize a raw (untyped) deploy config object.
 * Pure function, no I/O. Collects every validation error before throwing once.
 */
export function parseDeployConfig(raw: unknown): DeployConfig {
  const errors: string[] = [];

  if (!isPlainObject(raw)) {
    throw new DeployConfigError(['config: must be a JSON object']);
  }

  // region
  let region = '';
  if (!isNonEmptyString(raw.region)) {
    errors.push('region: is required');
  } else if (!REGION_PATTERN.test(raw.region)) {
    errors.push(`region: "${raw.region}" is not a valid AWS Region code`);
  } else {
    region = raw.region;
  }

  // model ids
  let defaultModelId = '';
  if (!isNonEmptyString(raw.defaultModelId)) {
    errors.push('defaultModelId: is required');
  } else if (!MODEL_ID_PATTERN.test(raw.defaultModelId)) {
    errors.push(`defaultModelId: "${raw.defaultModelId}" does not match the expected model ID format`);
  } else {
    defaultModelId = raw.defaultModelId;
  }

  let routerModelId = '';
  if (!isNonEmptyString(raw.routerModelId)) {
    errors.push('routerModelId: is required');
  } else if (!MODEL_ID_PATTERN.test(raw.routerModelId)) {
    errors.push(`routerModelId: "${raw.routerModelId}" does not match the expected model ID format`);
  } else {
    routerModelId = raw.routerModelId;
  }

  // blocked model ids (B18 configurable blocklist)
  let blockedModelIds: string[] = [];
  if (raw.blockedModelIds === undefined) {
    blockedModelIds = [];
  } else if (!isStringArray(raw.blockedModelIds)) {
    errors.push('blockedModelIds: must be an array of strings');
  } else {
    for (const modelId of raw.blockedModelIds) {
      if (!isNonEmptyString(modelId)) {
        errors.push('blockedModelIds: entries must not be empty');
      } else if (!MODEL_ID_PATTERN.test(modelId)) {
        errors.push(`blockedModelIds: "${modelId}" does not match the expected model ID format`);
      }
    }
    blockedModelIds = raw.blockedModelIds;
  }

  if (defaultModelId && blockedModelIds.includes(defaultModelId)) {
    errors.push(`defaultModelId: "${defaultModelId}" is also in blockedModelIds`);
  }
  if (routerModelId && blockedModelIds.includes(routerModelId)) {
    errors.push(`routerModelId: "${routerModelId}" is also in blockedModelIds`);
  }

  // email domain allowlist (B16)
  let allowedEmailDomains: string[] = [];
  if (!isStringArray(raw.allowedEmailDomains) || raw.allowedEmailDomains.length === 0) {
    errors.push('allowedEmailDomains: must be a non-empty array of strings');
  } else {
    for (const domain of raw.allowedEmailDomains) {
      if (!isNonEmptyString(domain)) {
        errors.push('allowedEmailDomains: entries must not be empty');
      } else if (!DOMAIN_PATTERN.test(domain)) {
        errors.push(`allowedEmailDomains: "${domain}" is not a valid domain`);
      }
    }
    allowedEmailDomains = raw.allowedEmailDomains;
  }

  // admin email (B50)
  let adminEmail = '';
  if (!isNonEmptyString(raw.adminEmail)) {
    errors.push('adminEmail: is required');
  } else if (!EMAIL_PATTERN.test(raw.adminEmail)) {
    errors.push(`adminEmail: "${raw.adminEmail}" is not a valid email address`);
  } else {
    adminEmail = raw.adminEmail;
  }

  const limits = parseLimits(raw.limits, errors);

  // web search flag (B44) — absent until `preflight.ts --write` sets it
  let webSearchEnabled = false;
  if (raw.webSearchEnabled !== undefined) {
    if (typeof raw.webSearchEnabled !== 'boolean') {
      errors.push('webSearchEnabled: must be a boolean');
    } else {
      webSearchEnabled = raw.webSearchEnabled;
    }
  }

  // B90: local-dev localhost callback/logout URLs, gated behind this flag (proposal, not a settled CORS/dev-origin decision)
  let allowLocalhostCallback = false;
  if (raw.allowLocalhostCallback !== undefined) {
    if (typeof raw.allowLocalhostCallback !== 'boolean') {
      errors.push('allowLocalhostCallback: must be a boolean');
    } else {
      allowLocalhostCallback = raw.allowLocalhostCallback;
    }
  }

  // F4/B30: tool Gateway policy mode — absent means LOG_ONLY (the safe start of the LOG_ONLY -> ENFORCE rollout)
  let toolPolicyMode: ToolPolicyMode = DEFAULT_TOOL_POLICY_MODE;
  if (raw.toolPolicyMode !== undefined) {
    if (!TOOL_POLICY_MODES.includes(raw.toolPolicyMode as ToolPolicyMode)) {
      errors.push(`toolPolicyMode: must be one of ${TOOL_POLICY_MODES.join(', ')}`);
    } else {
      toolPolicyMode = raw.toolPolicyMode as ToolPolicyMode;
    }
  }

  const repository = parseRepository(raw.repository, errors);

  if (errors.length > 0) {
    throw new DeployConfigError(errors);
  }

  return {
    region,
    defaultModelId,
    routerModelId,
    blockedModelIds,
    allowedEmailDomains,
    adminEmail,
    limits,
    webSearchEnabled,
    allowLocalhostCallback,
    toolPolicyMode,
    ...(repository ? { repository } : {}),
  };
}

/** Read and parse a deploy config JSON file from disk. Throws if the file is missing or invalid. */
export function loadDeployConfig(filePath: string): DeployConfig {
  const raw = fs.readFileSync(filePath, 'utf-8');
  return parseDeployConfig(JSON.parse(raw));
}

/** Like {@link loadDeployConfig}, but returns undefined instead of throwing when the file does not exist. */
export function tryLoadDeployConfig(filePath: string): DeployConfig | undefined {
  if (!fs.existsSync(filePath)) {
    return undefined;
  }
  return loadDeployConfig(filePath);
}

/**
 * Resolve the deploy config file path from CDK context (`-c ceiConfigFile=<path>`),
 * defaulting to `cei.deploy.json` relative to the `backend/` package root.
 */
export function resolveConfigPath(app: cdk.App): string {
  const contextValue = app.node.tryGetContext(CONTEXT_KEY);
  const fileName = typeof contextValue === 'string' && contextValue.length > 0
    ? contextValue
    : DEFAULT_CONFIG_FILE_NAME;

  if (path.isAbsolute(fileName)) {
    return fileName;
  }

  // backend/lib/config/deploy-config.ts -> backend/
  const backendRoot = path.resolve(__dirname, '..', '..');
  return path.resolve(backendRoot, fileName);
}
