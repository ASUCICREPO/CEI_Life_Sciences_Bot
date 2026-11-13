import { reportClientFailure } from './clientDiagnostics';

// Typed client for POST /account/delete (api.md "Account", F19/B109). Behind the accountDeletionEnabled
// flag; every call carries the Cognito access token, same as modelsClient.ts.

export interface AccountClient {
  /** 202 on both the first call and a repeat call; the account is disabled at once, deleted ~9h later. */
  deleteAccount(): Promise<{ status: string }>;
}

export interface AccountClientOptions {
  /** API root; `/api` locally (Vite proxy) or the deployed API stage URL. */
  baseUrl?: string;
  timeoutMs?: number;
  fetchImpl?: typeof fetch;
}

export type TokenProvider = () => Promise<string | null>;

const DEFAULT_TIMEOUT_MS = 15_000;

export class AccountClientError extends Error {
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
    this.name = 'AccountClientError';
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

// ADR: factory with an injected token provider | Rationale: matches modelsClient.ts/sessionsClient.ts so
// callers (and tests) pass AuthProvider's getAccessToken instead of reaching into React context.
export function createAccountClient(getToken: TokenProvider, options: AccountClientOptions = {}): AccountClient {
  const baseUrl = (options.baseUrl ?? '/api').replace(/\/+$/, '');
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const fetchImpl = options.fetchImpl ?? ((input: RequestInfo | URL, init?: RequestInit) => fetch(input, init));

  return {
    async deleteAccount() {
      let token: string | null;
      try { token = await getToken(); }
      catch (error) { reportClientFailure('auth', 'auth_error'); throw error; }
      if (!token) {
        throw new AccountClientError('You are signed out. Sign in again to continue.', { status: 401, code: 'unauthorized' });
      }
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), timeoutMs);
      let response: Response;
      try {
        response = await fetchImpl(`${baseUrl}/account/delete`, {
          method: 'POST',
          headers: { accept: 'application/json', authorization: `Bearer ${token}`, 'content-type': 'application/json' },
          body: JSON.stringify({}),
          signal: controller.signal,
        });
      } catch (error) {
        reportClientFailure('sessions', controller.signal.aborted ? 'timeout' : 'network');
        if (controller.signal.aborted) {
          throw new AccountClientError('The account service did not respond in time.', { retriable: true, cause: error });
        }
        throw new AccountClientError('Could not reach the account service.', { retriable: true, cause: error });
      } finally {
        clearTimeout(timeout);
      }

      const parsed: unknown = await response.json().catch((error: unknown) => {
        reportClientFailure('sessions', 'invalid_response');
        throw new AccountClientError('The account service returned an invalid response.', { status: response.status, cause: error });
      });
      if (!response.ok) {
        // api.md error envelope: { error: { code, message, retriable, request_id } }
        const envelope = asRecord(asRecord(parsed)?.error);
        if (response.status >= 500 || response.status === 429) {
          reportClientFailure('sessions', 'http_error', response.status, typeof envelope?.request_id === 'string' ? envelope.request_id : undefined);
        }
        throw new AccountClientError(
          typeof envelope?.message === 'string' ? envelope.message : `The account service returned an error (HTTP ${response.status}).`,
          {
            status: response.status,
            code: typeof envelope?.code === 'string' ? envelope.code : undefined,
            retriable: envelope?.retriable === true || response.status >= 500,
            requestId: typeof envelope?.request_id === 'string' ? envelope.request_id : undefined,
          },
        );
      }
      const record = asRecord(parsed);
      if (!record || typeof record.status !== 'string') {
        reportClientFailure('sessions', 'invalid_response');
        throw new AccountClientError('The account service returned an unexpected response shape.');
      }
      return { status: record.status };
    },
  };
}
