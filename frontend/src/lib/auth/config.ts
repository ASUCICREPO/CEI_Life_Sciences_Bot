export const AGENT_URL_KEYS = ['research', 'drafting', 'analysis', 'pipeline'] as const;
export type AgentUrlKey = (typeof AGENT_URL_KEYS)[number];

export interface RuntimeConfig {
  region: string;
  userPoolId: string;
  userPoolClientId: string;
  cognitoDomain: string;
  /** Sessions API base (https URL or same-origin path). Absent → clients use their relative '/api' default. */
  apiBaseUrl?: string;
  /** Enable only after the Research streaming endpoint has been deployed and verified. */
  researchStreamingEnabled?: boolean;
  /** Behind a flag, off by default (B109). Shows the Settings "Delete account" section. */
  accountDeletionEnabled?: boolean;
  /** Direct AgentCore invocation URLs per agent. Absent entries → the relative '/api/agents/<name>' dev proxy. */
  agentUrls?: Partial<Record<AgentUrlKey, string>>;
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0;
}

function isHttpsUrl(value: string): boolean {
  try {
    return new URL(value).protocol === 'https:';
  } catch {
    return false;
  }
}

function validateApiBaseUrl(value: unknown): string | undefined {
  if (value === undefined || value === null || value === '') return undefined;
  if (typeof value !== 'string' || !(value.startsWith('/') || isHttpsUrl(value))) {
    throw new Error('Invalid runtime config: "apiBaseUrl" must be an https URL or a path starting with "/".');
  }
  return value.replace(/\/+$/, '') || '/';
}

// Unknown keys and non-https entries are dropped (the caller falls back to the dev proxy path for that agent).
function validateAgentUrls(value: unknown): Partial<Record<AgentUrlKey, string>> | undefined {
  if (typeof value !== 'object' || value === null) return undefined;
  const candidate = value as Record<string, unknown>;
  const urls: Partial<Record<AgentUrlKey, string>> = {};
  for (const key of AGENT_URL_KEYS) {
    const url = candidate[key];
    if (isNonEmptyString(url) && isHttpsUrl(url)) urls[key] = url;
  }
  return Object.keys(urls).length > 0 ? urls : undefined;
}

function validateRuntimeConfig(data: unknown): RuntimeConfig {
  if (typeof data !== 'object' || data === null) {
    throw new Error('Invalid runtime config: expected a JSON object.');
  }
  const candidate = data as Record<string, unknown>;
  const { region, userPoolId, userPoolClientId, cognitoDomain } = candidate;

  if (!isNonEmptyString(region)) {
    throw new Error('Invalid runtime config: "region" must be a non-empty string.');
  }
  if (!isNonEmptyString(userPoolId)) {
    throw new Error('Invalid runtime config: "userPoolId" must be a non-empty string.');
  }
  if (!isNonEmptyString(userPoolClientId)) {
    throw new Error('Invalid runtime config: "userPoolClientId" must be a non-empty string.');
  }
  if (!isNonEmptyString(cognitoDomain) || !isHttpsUrl(cognitoDomain)) {
    throw new Error('Invalid runtime config: "cognitoDomain" must be an https URL.');
  }

  const config: RuntimeConfig = { region, userPoolId, userPoolClientId, cognitoDomain };
  const apiBaseUrl = validateApiBaseUrl(candidate.apiBaseUrl);
  if (apiBaseUrl) config.apiBaseUrl = apiBaseUrl;
  if (candidate.researchStreamingEnabled === true) config.researchStreamingEnabled = true;
  if (candidate.accountDeletionEnabled === true) config.accountDeletionEnabled = true;
  const agentUrls = validateAgentUrls(candidate.agentUrls);
  if (agentUrls) config.agentUrls = agentUrls;
  return config;
}

let configPromise: Promise<RuntimeConfig> | null = null;
let loadedConfig: RuntimeConfig | null = null;

// ADR: fetch /config.json at runtime, not build time | one static build works for every deployment
export function loadRuntimeConfig(): Promise<RuntimeConfig> {
  if (!configPromise) {
    configPromise = fetch('/config.json', { cache: 'no-store' })
      .then((response) => {
        if (!response.ok) {
          throw new Error(`Failed to fetch /config.json: HTTP ${response.status}`);
        }
        return response.json();
      })
      .then(validateRuntimeConfig)
      .then((config) => {
        loadedConfig = config;
        return config;
      })
      .catch((error: unknown) => {
        configPromise = null;
        throw error instanceof Error ? error : new Error('Failed to load runtime config.');
      });
  }
  return configPromise;
}

// ADR: synchronous read of the resolved config | Rationale: AuthProvider awaits loadRuntimeConfig before sign-in, so
// non-React modules (agentClient) can read it without changing their call signatures; null before it has loaded.
export function getLoadedRuntimeConfig(): RuntimeConfig | null {
  return loadedConfig;
}

/** Test-only: clear the cached config so each test starts from a fresh load. */
export function resetRuntimeConfigForTests(): void {
  configPromise = null;
  loadedConfig = null;
}
