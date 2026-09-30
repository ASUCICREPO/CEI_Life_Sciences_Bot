export interface RuntimeConfig {
  region: string;
  userPoolId: string;
  userPoolClientId: string;
  cognitoDomain: string;
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

  return { region, userPoolId, userPoolClientId, cognitoDomain };
}

let configPromise: Promise<RuntimeConfig> | null = null;

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
      .catch((error: unknown) => {
        configPromise = null;
        throw error instanceof Error ? error : new Error('Failed to load runtime config.');
      });
  }
  return configPromise;
}
