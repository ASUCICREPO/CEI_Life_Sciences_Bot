import { reportClientFailure } from './clientDiagnostics';

// Typed client for the Models/Settings REST API (models-settings-handler, api.md "Settings").
// Every call carries the Cognito access token; the backend derives the user from the token's `sub` only.

export interface ModelOption {
  model_id: string;
  label: string;
}

export type SettingsMode = 'science' | 'business';

export interface UserSettings {
  default_model_id: string | null;
  default_mode: SettingsMode | null;
  /** Source IDs only, never key material (B18/B20). */
  configured_sources: string[];
}

export interface SettingsPatch {
  default_model_id?: string | null;
  default_mode?: SettingsMode | null;
}

export interface ModelsClient {
  /** Server-discovered, blocklist-filtered Bedrock models available for selection. */
  listModels(): Promise<ModelOption[]>;
  getSettings(): Promise<UserSettings>;
  /** 400 invalid_request if the model is blocklisted/undiscovered or the mode is invalid. */
  updateSettings(patch: SettingsPatch): Promise<UserSettings>;
}

export interface ModelsClientOptions {
  /** API root; `/api` locally (Vite proxy) or the deployed API stage URL. */
  baseUrl?: string;
  timeoutMs?: number;
  fetchImpl?: typeof fetch;
}

export type TokenProvider = () => Promise<string | null>;

const DEFAULT_TIMEOUT_MS = 15_000;

export class ModelsClientError extends Error {
  status?: number;
  code?: string;
  retriable: boolean;
  requestId?: string;
  cause?: unknown;
  constructor(
    message: string,
    details: { status?: number; code?: string; retriable?: boolean; requestId?: string; cause?: unknown } = {},
  ) {
    super(message);
    this.name = 'ModelsClientError';
    this.status = details.status;
    this.code = details.code;
    this.retriable = details.retriable ?? false;
    this.requestId = details.requestId;
    this.cause = details.cause;
  }
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
}

function toModelOption(raw: unknown): ModelOption | null {
  const record = asRecord(raw);
  if (!record || typeof record.model_id !== 'string' || typeof record.label !== 'string') return null;
  return { model_id: record.model_id, label: record.label };
}

function toUserSettings(raw: unknown): UserSettings {
  const record = asRecord(raw);
  if (!record) {
    reportClientFailure('sessions', 'invalid_response');
    throw new ModelsClientError('The settings service returned an unexpected response shape.');
  }
  return {
    default_model_id: typeof record.default_model_id === 'string' ? record.default_model_id : null,
    default_mode: record.default_mode === 'science' || record.default_mode === 'business' ? record.default_mode : null,
    configured_sources: Array.isArray(record.configured_sources)
      ? record.configured_sources.filter((item): item is string => typeof item === 'string')
      : [],
  };
}

// ADR: factory with an injected token provider | Rationale: matches sessionsClient.ts so callers (and
// tests) pass AuthProvider's getAccessToken instead of reaching into React context or a global singleton.
export function createModelsClient(getToken: TokenProvider, options: ModelsClientOptions = {}): ModelsClient {
  const baseUrl = (options.baseUrl ?? '/api').replace(/\/+$/, '');
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const fetchImpl = options.fetchImpl ?? ((input: RequestInfo | URL, init?: RequestInit) => fetch(input, init));

  async function request(method: 'GET' | 'PUT', path: string, body?: unknown): Promise<unknown> {
    let token: string | null;
    try { token = await getToken(); }
    catch (error) { reportClientFailure('auth', 'auth_error'); throw error; }
    if (!token) {
      throw new ModelsClientError('You are signed out. Sign in again to continue.', { status: 401, code: 'unauthorized' });
    }
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), timeoutMs);
    let response: Response;
    try {
      response = await fetchImpl(`${baseUrl}${path}`, {
        method,
        headers: {
          accept: 'application/json',
          authorization: `Bearer ${token}`,
          ...(body !== undefined ? { 'content-type': 'application/json' } : {}),
        },
        body: body !== undefined ? JSON.stringify(body) : undefined,
        signal: controller.signal,
      });
    } catch (error) {
      reportClientFailure('sessions', controller.signal.aborted ? 'timeout' : 'network');
      if (controller.signal.aborted) {
        throw new ModelsClientError('The settings service did not respond in time.', { retriable: true, cause: error });
      }
      throw new ModelsClientError('Could not reach the settings service.', { retriable: true, cause: error });
    } finally {
      clearTimeout(timeout);
    }

    const parsed: unknown = await response.json().catch((error: unknown) => {
      reportClientFailure('sessions', 'invalid_response');
      throw new ModelsClientError('The settings service returned an invalid response.', { status: response.status, cause: error });
    });
    if (!response.ok) {
      // api.md error envelope: { error: { code, message, retriable, request_id } }
      const envelope = asRecord(asRecord(parsed)?.error);
      if (response.status >= 500 || response.status === 429) {
        reportClientFailure('sessions', 'http_error', response.status, typeof envelope?.request_id === 'string' ? envelope.request_id : undefined);
      }
      throw new ModelsClientError(
        typeof envelope?.message === 'string' ? envelope.message : `The settings service returned an error (HTTP ${response.status}).`,
        {
          status: response.status,
          code: typeof envelope?.code === 'string' ? envelope.code : undefined,
          retriable: envelope?.retriable === true || response.status >= 500,
          requestId: typeof envelope?.request_id === 'string' ? envelope.request_id : undefined,
        },
      );
    }
    return parsed;
  }

  return {
    async listModels() {
      const raw = await request('GET', '/models');
      const record = asRecord(raw);
      if (!record || !Array.isArray(record.models)) {
        reportClientFailure('sessions', 'invalid_response');
        throw new ModelsClientError('The models service returned an unexpected response shape.');
      }
      return record.models.map(toModelOption).filter((model): model is ModelOption => model !== null);
    },
    async getSettings() {
      return toUserSettings(await request('GET', '/settings'));
    },
    async updateSettings(patch) {
      return toUserSettings(await request('PUT', '/settings', patch));
    },
  };
}
