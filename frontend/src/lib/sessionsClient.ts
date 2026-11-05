import { safeParseCEIUISurface, type CEIUISurface } from './cei-ui/schema';

// Typed client for the Session REST API (session-metadata / session-history Lambdas behind API Gateway).
// Every call carries the Cognito access token; the backend derives the user from the token's `sub` only.

/** `activity` = last time the user sent a message in the chat (the default history order). */
export type SessionSort = 'activity' | 'updated' | 'created';
export type TitleSource = 'prompt' | 'generated' | 'user';
export type SessionMode = 'science' | 'business';
export type SessionStatus = 'active' | 'archived';

export interface SessionSummary {
  session_id: string;
  title: string;
  title_source: TitleSource;
  project?: string | null;
  status: SessionStatus;
  agent_type?: string | null;
  model_id?: string | null;
  /** Drafting sessions: the document type the chat started with. */
  document_type?: string | null;
  /** Analysis sessions: the analysis type the chat started with. */
  analysis_type?: string | null;
  mode?: SessionMode | null;
  message_count: number;
  created_at: string;
  updated_at: string;
  /** Last time the user sent a message here; moves an old chat back to the top of the history list. */
  last_activity_at?: string | null;
}

export interface CreatedSession {
  session_id: string;
  title: string;
  title_source: TitleSource;
  mode: SessionMode;
  status: SessionStatus;
  created_at: string;
}

/** Session fields the history envelope carries so the UI can reopen the chat in the right agent. */
export interface SessionRef {
  session_id: string;
  title: string;
  title_source: TitleSource;
  agent_type?: string | null;
  mode?: SessionMode | null;
  model_id?: string | null;
  document_type?: string | null;
  analysis_type?: string | null;
}

export interface Page<T> {
  items: T[];
  next_cursor: string | null;
  has_more: boolean;
}

export type HistoryRole = 'human' | 'ai' | 'tool';

export interface HistoryContentBlock {
  block_type: 'text' | 'ui' | 'a2ui' | 'thinking';
  body: string | Record<string, unknown>;
}

export interface HistoryMessage {
  message_id: string;
  role: HistoryRole;
  sequence_number: number;
  timestamp: string;
  /** Text blocks joined; what the chat bubble renders. */
  text: string;
  /** First `ui` block, validated against the CEI UI schema (1.0 / 1.1). */
  ui?: CEIUISurface;
  ui_error?: string;
  citations: Record<string, unknown>[];
  /** True when the backend trimmed an oversized message before storing it. */
  truncated: boolean;
  content_blocks: HistoryContentBlock[];
}

export interface HistoryPage extends Page<HistoryMessage> {
  session: SessionRef;
}

export interface ListSessionsOptions {
  sort?: SessionSort;
  limit?: number;
  cursor?: string | null;
  /** Title search (case-insensitive substring; results in title order). Max 100 characters. */
  q?: string | null;
}

export interface LoadHistoryOptions {
  limit?: number;
  cursor?: string | null;
}

export interface SessionsClient {
  listSessions(options?: ListSessionsOptions): Promise<Page<SessionSummary>>;
  getSession(sessionId: string): Promise<SessionSummary>;
  createSession(input: { title: string; mode: SessionMode; model_id?: string }): Promise<CreatedSession>;
  updateSession(sessionId: string, patch: { title?: string; mode?: SessionMode }): Promise<SessionSummary>;
  /** Newest page first; items within a page are chronological. Pass `next_cursor` to load older messages. */
  loadHistory(sessionId: string, options?: LoadHistoryOptions): Promise<HistoryPage>;
}

export interface SessionsClientOptions {
  /** API root; `/api` locally (Vite proxy) or the deployed API stage URL. */
  baseUrl?: string;
  timeoutMs?: number;
  fetchImpl?: typeof fetch;
}

export type TokenProvider = () => Promise<string | null>;

const DEFAULT_TIMEOUT_MS = 15_000;

export class SessionsClientError extends Error {
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
    this.name = 'SessionsClientError';
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

function toPage<T>(raw: unknown, mapItem: (item: unknown) => T): Page<T> {
  const record = asRecord(raw);
  if (!record || !Array.isArray(record.items)) {
    throw new SessionsClientError('The session service returned an unexpected response shape.');
  }
  const nextCursor = typeof record.next_cursor === 'string' ? record.next_cursor : null;
  return { items: record.items.map(mapItem), next_cursor: nextCursor, has_more: nextCursor !== null };
}

export function toHistoryMessage(raw: unknown): HistoryMessage {
  const record = asRecord(raw) ?? {};
  const blocks = (Array.isArray(record.content_blocks) ? record.content_blocks : [])
    .map(asRecord)
    .filter((b): b is Record<string, unknown> => b !== null) as unknown as HistoryContentBlock[];
  const message: HistoryMessage = {
    message_id: String(record.message_id ?? ''),
    role: (record.role === 'ai' || record.role === 'tool' ? record.role : 'human') as HistoryRole,
    sequence_number: typeof record.sequence_number === 'number' ? record.sequence_number : 0,
    timestamp: typeof record.timestamp === 'string' ? record.timestamp : '',
    text: blocks
      .filter((b) => b.block_type === 'text' && typeof b.body === 'string')
      .map((b) => b.body as string)
      .join('\n\n'),
    citations: Array.isArray(record.citations) ? (record.citations as Record<string, unknown>[]) : [],
    truncated: record.truncated === true,
    content_blocks: blocks,
  };
  const uiBlock = blocks.find((b) => b.block_type === 'ui');
  if (uiBlock) {
    const parsed = safeParseCEIUISurface(uiBlock.body);
    if (parsed.success) {
      message.ui = parsed.data;
    } else {
      message.ui_error = 'This message has a rich view that could not be validated.';
    }
  }
  return message;
}

function toSessionRef(raw: unknown, sessionId: string): SessionRef {
  const record = asRecord(raw) ?? {};
  return {
    session_id: typeof record.session_id === 'string' ? record.session_id : sessionId,
    title: typeof record.title === 'string' ? record.title : '',
    title_source: (record.title_source ?? 'prompt') as TitleSource,
    agent_type: typeof record.agent_type === 'string' ? record.agent_type : null,
    mode: (record.mode === 'science' || record.mode === 'business' ? record.mode : null) as SessionMode | null,
    model_id: typeof record.model_id === 'string' ? record.model_id : null,
    document_type: typeof record.document_type === 'string' ? record.document_type : null,
    analysis_type: typeof record.analysis_type === 'string' ? record.analysis_type : null,
  };
}

function toSessionSummary(raw: unknown): SessionSummary {
  const record = asRecord(raw);
  if (!record || typeof record.session_id !== 'string') {
    throw new SessionsClientError('The session service returned an unexpected session shape.');
  }
  return {
    ...(record as unknown as SessionSummary),
    title_source: (record.title_source ?? 'prompt') as TitleSource,
    message_count: typeof record.message_count === 'number' ? record.message_count : 0,
  };
}

function query(params: Record<string, string | number | null | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== '') search.set(key, String(value));
  }
  const encoded = search.toString();
  return encoded ? `?${encoded}` : '';
}

// ADR: factory with an injected token provider | Rationale: callers (and tests) pass AuthProvider's
// getAccessToken, so this module never reaches into React context or a global auth singleton.
export function createSessionsClient(getToken: TokenProvider, options: SessionsClientOptions = {}): SessionsClient {
  const baseUrl = (options.baseUrl ?? '/api').replace(/\/+$/, '');
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const fetchImpl = options.fetchImpl ?? ((input: RequestInfo | URL, init?: RequestInit) => fetch(input, init));

  async function request(method: 'GET' | 'POST' | 'PATCH', path: string, body?: unknown): Promise<unknown> {
    const token = await getToken();
    if (!token) {
      throw new SessionsClientError('You are signed out. Sign in again to see your chats.', {
        status: 401,
        code: 'unauthorized',
      });
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
      if (controller.signal.aborted) {
        throw new SessionsClientError('The session service did not respond in time.', { retriable: true, cause: error });
      }
      throw new SessionsClientError('Could not reach the session service.', { retriable: true, cause: error });
    } finally {
      clearTimeout(timeout);
    }

    const parsed: unknown = await response.json().catch((error: unknown) => {
      throw new SessionsClientError('The session service returned an invalid response.', {
        status: response.status,
        cause: error,
      });
    });
    if (!response.ok) {
      // api.md error envelope: { error: { code, message, retriable, request_id } }
      const envelope = asRecord(asRecord(parsed)?.error);
      throw new SessionsClientError(
        typeof envelope?.message === 'string'
          ? envelope.message
          : `The session service returned an error (HTTP ${response.status}).`,
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

  const sessionPath = (sessionId: string) => `/sessions/${encodeURIComponent(sessionId)}`;

  return {
    async listSessions({ sort = 'activity', limit, cursor, q } = {}) {
      const search = q?.trim() || null;
      return toPage(await request('GET', `/sessions${query({ sort, limit, q: search, cursor })}`), toSessionSummary);
    },
    async getSession(sessionId) {
      return toSessionSummary(await request('GET', sessionPath(sessionId)));
    },
    async createSession(input) {
      return (await request('POST', '/sessions', input)) as CreatedSession;
    },
    async updateSession(sessionId, patch) {
      return toSessionSummary(await request('PATCH', sessionPath(sessionId), patch));
    },
    async loadHistory(sessionId, { limit, cursor } = {}) {
      const raw = await request('GET', `${sessionPath(sessionId)}/history${query({ limit, cursor })}`);
      return { ...toPage(raw, toHistoryMessage), session: toSessionRef(asRecord(raw)?.session, sessionId) };
    },
  };
}
