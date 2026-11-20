import { asRecord, parseApiErrorEnvelope } from './apiErrorEnvelope';
import { reportClientFailure } from './clientDiagnostics';

// Typed client for POST /profile (founder onboarding: given_name/family_name/birthdate on the Cognito
// user). Every call carries the Cognito access token, same factory/token-provider style as accountClient.ts.

export interface ProfileInput {
  given_name: string;
  family_name: string;
  /** "YYYY-MM-DD". */
  birthdate: string;
}

export interface Profile {
  given_name: string;
  family_name: string;
  birthdate: string;
}

export interface SubmitProfileResult {
  status: string;
  profile: Profile;
}

export interface ProfileClient {
  /**
   * 200 on success. 409 profile_exists and 403 age_restricted are both expected outcomes the caller
   * must branch on (see ProfileClientError.code), not just failure noise.
   */
  submitProfile(input: ProfileInput): Promise<SubmitProfileResult>;
}

export interface ProfileClientOptions {
  /** API root; `/api` locally (Vite proxy) or the deployed API stage URL. */
  baseUrl?: string;
  timeoutMs?: number;
  fetchImpl?: typeof fetch;
}

export type TokenProvider = () => Promise<string | null>;

const DEFAULT_TIMEOUT_MS = 15_000;

export class ProfileClientError extends Error {
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
    this.name = 'ProfileClientError';
    this.status = details.status;
    this.code = details.code;
    this.retriable = details.retriable ?? false;
    this.requestId = details.requestId;
    this.cause = details.cause;
  }
}

function toProfile(raw: unknown): Profile | null {
  const record = asRecord(raw);
  if (!record || typeof record.given_name !== 'string' || typeof record.family_name !== 'string' || typeof record.birthdate !== 'string') {
    return null;
  }
  return { given_name: record.given_name, family_name: record.family_name, birthdate: record.birthdate };
}

// ADR: factory with an injected token provider | Rationale: matches accountClient.ts/modelsClient.ts so
// callers (and tests) pass AuthProvider's getAccessToken instead of reaching into React context.
export function createProfileClient(getToken: TokenProvider, options: ProfileClientOptions = {}): ProfileClient {
  const baseUrl = (options.baseUrl ?? '/api').replace(/\/+$/, '');
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const fetchImpl = options.fetchImpl ?? ((input: RequestInfo | URL, init?: RequestInit) => fetch(input, init));

  return {
    async submitProfile(input) {
      let token: string | null;
      try { token = await getToken(); }
      catch (error) { reportClientFailure('auth', 'auth_error'); throw error; }
      if (!token) {
        throw new ProfileClientError('You are signed out. Sign in again to continue.', { status: 401, code: 'unauthorized' });
      }
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), timeoutMs);
      let response: Response;
      try {
        response = await fetchImpl(`${baseUrl}/profile`, {
          method: 'POST',
          headers: { accept: 'application/json', authorization: `Bearer ${token}`, 'content-type': 'application/json' },
          body: JSON.stringify(input),
          signal: controller.signal,
        });
      } catch (error) {
        reportClientFailure('sessions', controller.signal.aborted ? 'timeout' : 'network');
        if (controller.signal.aborted) {
          throw new ProfileClientError('The profile service did not respond in time.', { retriable: true, cause: error });
        }
        throw new ProfileClientError('Could not reach the profile service.', { retriable: true, cause: error });
      } finally {
        clearTimeout(timeout);
      }

      const parsed: unknown = await response.json().catch((error: unknown) => {
        reportClientFailure('sessions', 'invalid_response');
        throw new ProfileClientError('The profile service returned an invalid response.', { status: response.status, cause: error });
      });
      if (!response.ok) {
        const envelope = parseApiErrorEnvelope(parsed, response, `The profile service returned an error (HTTP ${response.status}).`);
        if (response.status >= 500 || response.status === 429) {
          reportClientFailure('sessions', 'http_error', response.status, envelope.requestId);
        }
        throw new ProfileClientError(envelope.message, {
          status: response.status,
          code: envelope.code,
          retriable: envelope.retriable,
          requestId: envelope.requestId,
        });
      }
      const record = asRecord(parsed);
      const profile = toProfile(record?.profile);
      if (!record || typeof record.status !== 'string' || !profile) {
        reportClientFailure('sessions', 'invalid_response');
        throw new ProfileClientError('The profile service returned an unexpected response shape.');
      }
      return { status: record.status, profile };
    },
  };
}
