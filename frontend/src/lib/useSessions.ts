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

export function useSessions({ sort = 'updated', limit = 20, client }: UseSessionsOptions = {}): UseSessionsResult {
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
