import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useAuth } from './auth/AuthProvider';
import {
  createSessionsClient,
  SessionsClientError,
  type SessionSort,
  type SessionSummary,
  type SessionsClient,
} from './sessionsClient';

/**
 * One SessionsClient per access-token provider and API base; every call carries the caller's Cognito token.
 * The base comes from /config.json `apiBaseUrl` (the deployed API Gateway stage); without it the client keeps
 * its relative '/api' default, which the Vite dev proxy forwards.
 */
export function useSessionsClient(): SessionsClient {
  const { getAccessToken, config } = useAuth();
  const baseUrl = config?.apiBaseUrl;
  return useMemo(() => createSessionsClient(getAccessToken, { baseUrl }), [getAccessToken, baseUrl]);
}

export interface UseSessionsOptions {
  sort?: SessionSort;
  limit?: number;
  /** Inject a client (tests); defaults to one bound to the auth context. */
  client?: SessionsClient;
}

export interface UseSessionsResult {
  sessions: SessionSummary[];
  isLoading: boolean;
  isLoadingMore: boolean;
  error: string | null;
  hasMore: boolean;
  loadMore: () => void;
  reload: () => void;
}

function toErrorMessage(error: unknown): string {
  if (error instanceof SessionsClientError) return error.message;
  return 'Could not load your chats.';
}

export function useSessions({ sort = 'activity', limit = 20, client }: UseSessionsOptions = {}): UseSessionsResult {
  const contextClient = useSessionsClient();
  const sessionsClient = client ?? contextClient;
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isLoadingMore, setIsLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reloadToken, setReloadToken] = useState(0);
  // Guards against a stale response landing after sort/client changed.
  const generation = useRef(0);

  useEffect(() => {
    const current = ++generation.current;
    setIsLoading(true);
    setError(null);
    sessionsClient
      .listSessions({ sort, limit })
      .then((page) => {
        if (current !== generation.current) return;
        setSessions(page.items);
        setCursor(page.next_cursor);
      })
      .catch((loadError: unknown) => {
        if (current !== generation.current) return;
        setSessions([]);
        setCursor(null);
        setError(toErrorMessage(loadError));
      })
      .finally(() => {
        if (current === generation.current) setIsLoading(false);
      });
  }, [sessionsClient, sort, limit, reloadToken]);

  const loadMore = useCallback(() => {
    if (!cursor || isLoadingMore) return;
    const current = generation.current;
    setIsLoadingMore(true);
    sessionsClient
      .listSessions({ sort, limit, cursor })
      .then((page) => {
        if (current !== generation.current) return;
        setSessions((prev) => [...prev, ...page.items]);
        setCursor(page.next_cursor);
      })
      .catch((loadError: unknown) => {
        if (current !== generation.current) return;
        setError(toErrorMessage(loadError));
      })
      .finally(() => {
        if (current === generation.current) setIsLoadingMore(false);
      });
  }, [cursor, isLoadingMore, sessionsClient, sort, limit]);

  const reload = useCallback(() => setReloadToken((token) => token + 1), []);

  return { sessions, isLoading, isLoadingMore, error, hasMore: cursor !== null, loadMore, reload };
}

/** Chat history page size (matches the API's GET /sessions default). */
export const HISTORY_PAGE_SIZE = 25;

export interface UseSessionPagesOptions {
  sort?: SessionSort;
  /** Title search; blank means the plain list. Changing it (or the sort) returns to page 1. */
  query?: string;
  pageSize?: number;
  client?: SessionsClient;
}

export interface UseSessionPagesResult {
  sessions: SessionSummary[];
  /** 1-based page number. */
  page: number;
  hasNext: boolean;
  hasPrevious: boolean;
  next: () => void;
  previous: () => void;
  isLoading: boolean;
  error: string | null;
  reload: () => void;
}

/**
 * Numbered pages over the cursor API. ADR: keep a stack of the cursors that opened each page | Rationale: the API
 * is cursor-based (offsets would skip / repeat rows when a chat moves to the top after a new message), so "previous"
 * re-reads the page from the cursor that first opened it.
 */
export function useSessionPages({
  sort = 'activity',
  query = '',
  pageSize = HISTORY_PAGE_SIZE,
  client,
}: UseSessionPagesOptions = {}): UseSessionPagesResult {
  const contextClient = useSessionsClient();
  const sessionsClient = client ?? contextClient;
  const q = query.trim();
  // cursors[i] opens page i + 1 (page 1 has none). The stack belongs to one (sort, search, size) view: a new
  // search or order starts again at page 1 without ever sending an old view's cursor.
  const viewKey = `${sort}\u0000${q}\u0000${pageSize}`;
  const [nav, setNav] = useState<{ key: string; cursors: (string | null)[] }>({ key: viewKey, cursors: [null] });
  if (nav.key !== viewKey) {
    // Reset (not just mask) the stack: otherwise going back to an earlier view (e.g. clearing a search) would
    // reopen that view's old page instead of page 1. setState during render is React's "adjust state on prop
    // change" pattern; it re-renders before committing.
    setNav({ key: viewKey, cursors: [null] });
  }
  const cursors = nav.key === viewKey ? nav.cursors : [null];
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reloadToken, setReloadToken] = useState(0);
  const generation = useRef(0);

  const cursor = cursors[cursors.length - 1];
  useEffect(() => {
    const current = ++generation.current;
    setIsLoading(true);
    setError(null);
    sessionsClient
      .listSessions({ sort, limit: pageSize, cursor, q: q || null })
      .then((result) => {
        if (current !== generation.current) return;
        setSessions(result.items);
        setNextCursor(result.next_cursor);
      })
      .catch((loadError: unknown) => {
        if (current !== generation.current) return;
        setSessions([]);
        setNextCursor(null);
        setError(toErrorMessage(loadError));
      })
      .finally(() => {
        if (current === generation.current) setIsLoading(false);
      });
  }, [sessionsClient, sort, q, pageSize, cursor, reloadToken]);

  const next = useCallback(() => {
    if (!nextCursor || isLoading) return;
    setNav({ key: viewKey, cursors: [...cursors, nextCursor] });
  }, [nextCursor, isLoading, viewKey, cursors]);

  const previous = useCallback(() => {
    if (isLoading || cursors.length < 2) return;
    setNav({ key: viewKey, cursors: cursors.slice(0, -1) });
  }, [isLoading, viewKey, cursors]);

  const reload = useCallback(() => setReloadToken((token) => token + 1), []);

  return {
    sessions,
    page: cursors.length,
    hasNext: nextCursor !== null,
    hasPrevious: cursors.length > 1,
    next,
    previous,
    isLoading,
    error,
    reload,
  };
}

/** Route state the chat page reads to resume a session; `sessionId` is the stable contract. */
export interface ResumeSessionState {
  sessionId: string;
  agentType?: string | null;
  /** Stored turn context, so the chat reopens in the same analysis task / drafting document type. */
  analysisType?: string | null;
  documentType?: string | null;
}

export function resumeState(session: SessionSummary): ResumeSessionState {
  return {
    sessionId: session.session_id,
    agentType: session.agent_type ?? null,
    analysisType: session.analysis_type ?? null,
    documentType: session.document_type ?? null,
  };
}
