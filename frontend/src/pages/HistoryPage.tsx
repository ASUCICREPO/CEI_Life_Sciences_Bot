import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  AlertCircle,
  ArrowRight,
  Briefcase,
  ChevronLeft,
  ChevronRight,
  FlaskConical,
  Loader2,
  MessageSquare,
  Search,
  X,
} from 'lucide-react';
import { HISTORY_PAGE_SIZE, resumeState, useSessionPages } from '../lib/useSessions';
import type { SessionSort, SessionSummary, SessionsClient } from '../lib/sessionsClient';

/** Matches the API's `q` limit (chat_metadata.SEARCH_QUERY_MAX); longer input is rejected with a 400. */
export const SEARCH_MAX_LENGTH = 100;
/** Wait this long after the last keystroke before searching, so typing doesn't fire one request per key. */
export const SEARCH_DEBOUNCE_MS = 300;

interface HistoryPageProps {
  /** Injectable for tests; defaults to a client bound to the signed-in user's token. */
  client?: SessionsClient;
  /** Injectable for tests; 0 searches on every change. */
  searchDebounceMs?: number;
}

function timestampFor(session: SessionSummary, sort: SessionSort): { label: string; value: string | null | undefined } {
  if (sort === 'created') return { label: 'Created', value: session.created_at };
  if (sort === 'updated') return { label: 'Last updated', value: session.updated_at };
  return { label: 'Last used', value: session.last_activity_at ?? session.updated_at };
}

export function HistoryPage({ client, searchDebounceMs = SEARCH_DEBOUNCE_MS }: HistoryPageProps = {}) {
  const navigate = useNavigate();
  const [sort, setSort] = useState<SessionSort>('activity');
  const [searchInput, setSearchInput] = useState('');
  const [query, setQuery] = useState('');

  // ADR: debounce in the page, not the hook | Rationale: the hook stays a pure (sort, query, page) -> rows view,
  // and changing `query` there already resets to page 1.
  useEffect(() => {
    const next = searchInput.trim();
    if (searchDebounceMs <= 0) {
      setQuery(next);
      return;
    }
    const timer = window.setTimeout(() => setQuery(next), searchDebounceMs);
    return () => window.clearTimeout(timer);
  }, [searchInput, searchDebounceMs]);

  const { sessions, page, hasNext, hasPrevious, next, previous, isLoading, error, reload } = useSessionPages({
    sort,
    query,
    pageSize: HISTORY_PAGE_SIZE,
    client,
  });
  const searching = query.length > 0;

  return (
    <div className="max-w-4xl mx-auto px-6 py-8">
      <div data-tour="history-overview" className="mb-6 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-text-primary">Chat history</h1>
          <p className="text-sm text-text-secondary mt-1">Return to recent conversations and continue where you left off.</p>
        </div>
        <label className="flex items-center gap-2 text-xs text-text-secondary">
          Sort by
          <select
            value={sort}
            onChange={(event) => setSort(event.target.value as SessionSort)}
            disabled={searching}
            title={searching ? 'Search results are listed by title' : undefined}
            className="px-2 py-1 rounded-md border border-border-subtle bg-surface-elevated text-xs text-text-primary disabled:opacity-60"
          >
            <option value="activity">Last activity</option>
            <option value="updated">Last updated</option>
            <option value="created">Date created</option>
          </select>
        </label>
      </div>

      <form role="search" className="mb-5" onSubmit={(event) => { event.preventDefault(); setQuery(searchInput.trim()); }}>
        <label htmlFor="history-search" className="sr-only">Search chats by title</label>
        <div className="relative">
          <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-text-tertiary" aria-hidden="true" />
          <input
            id="history-search"
            type="search"
            value={searchInput}
            maxLength={SEARCH_MAX_LENGTH}
            onChange={(event) => setSearchInput(event.target.value)}
            placeholder="Search chats by title"
            className="w-full pl-9 pr-9 py-2 rounded-lg border border-border-subtle bg-surface-elevated text-sm text-text-primary placeholder:text-text-tertiary"
          />
          {searchInput && (
            <button
              type="button"
              onClick={() => { setSearchInput(''); setQuery(''); }}
              aria-label="Clear search"
              className="absolute right-2 top-1/2 -translate-y-1/2 p-1 text-text-tertiary hover:text-text-primary"
            >
              <X size={14} aria-hidden="true" />
            </button>
          )}
        </div>
      </form>

      {isLoading && (
        <div role="status" className="flex items-center gap-2 text-sm text-text-secondary py-8 justify-center">
          <Loader2 size={16} className="animate-spin text-cei-blue" aria-hidden="true" />
          <span>{searching ? 'Searching your chats…' : 'Loading your chats…'}</span>
        </div>
      )}

      {!isLoading && error && (
        <div role="alert" className="flex items-center gap-3 p-4 rounded-xl border border-evidence-weak/40 bg-surface-elevated">
          <AlertCircle size={16} className="text-evidence-weak shrink-0" aria-hidden="true" />
          <p className="flex-1 text-sm text-text-primary">{error}</p>
          <button onClick={reload} className="text-xs font-medium text-cei-blue-light hover:text-cei-blue">Retry</button>
        </div>
      )}

      {!isLoading && !error && sessions.length === 0 && searching && (
        <div className="text-center py-12">
          <Search size={24} className="text-text-tertiary mx-auto mb-3" aria-hidden="true" />
          <p className="text-sm text-text-primary font-medium">No chats match “{query}”</p>
          <p className="text-xs text-text-secondary mt-1">Try a different word from the chat title.</p>
        </div>
      )}

      {!isLoading && !error && sessions.length === 0 && !searching && page === 1 && (
        <div className="text-center py-12">
          <MessageSquare size={24} className="text-text-tertiary mx-auto mb-3" aria-hidden="true" />
          <p className="text-sm text-text-primary font-medium">No chats yet</p>
          <p className="text-xs text-text-secondary mt-1">Start a task and your conversations will appear here.</p>
          <button onClick={() => navigate('/tasks')} className="mt-4 text-xs font-medium text-cei-blue-light hover:text-cei-blue">Start a new task</button>
        </div>
      )}

      {!isLoading && sessions.length > 0 && (
        <div className="space-y-3">
          {sessions.map((session) => {
            const ModeIcon = session.mode === 'business' ? Briefcase : FlaskConical;
            const when = timestampFor(session, sort);
            return (
              <button
                key={session.session_id}
                onClick={() => navigate('/tasks', { state: resumeState(session) })}
                className="w-full text-left p-5 rounded-xl border border-border-subtle bg-surface-elevated hover:border-cei-blue-light/40 hover:shadow-sm transition-all group"
              >
                <div className="flex items-start gap-4">
                  <div className="w-10 h-10 rounded-xl bg-cei-blue/5 flex items-center justify-center flex-shrink-0">
                    <MessageSquare size={18} className="text-cei-blue" />
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <h2 className="text-sm font-semibold text-text-primary group-hover:text-cei-blue transition-colors">{session.title || 'Untitled chat'}</h2>
                      {session.mode && (
                        <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-surface-panel text-[10px] font-medium text-text-secondary capitalize"><ModeIcon size={10} />{session.mode}</span>
                      )}
                    </div>
                    <p className="text-sm text-text-secondary mt-1">
                      {session.message_count} {session.message_count === 1 ? 'message' : 'messages'}
                      {session.agent_type ? ` · ${session.agent_type}` : ''}
                    </p>
                    {when.value && (
                      <p className="text-xs text-text-tertiary mt-2">
                        {when.label} {new Date(when.value).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })}
                      </p>
                    )}
                  </div>
                  <ArrowRight size={15} className="text-text-tertiary group-hover:text-cei-blue-light mt-1 transition-colors" />
                </div>
              </button>
            );
          })}
        </div>
      )}

      {!isLoading && !error && (hasPrevious || hasNext) && (
        <nav aria-label="Chat history pages" className="flex items-center justify-between pt-4">
          <button
            onClick={previous}
            disabled={!hasPrevious}
            className="flex items-center gap-1 px-3 py-1.5 rounded-lg border border-border-subtle text-xs font-medium text-text-secondary hover:text-cei-blue disabled:opacity-40"
          >
            <ChevronLeft size={12} aria-hidden="true" /> Previous
          </button>
          <span className="text-xs text-text-tertiary" aria-live="polite">Page {page}</span>
          <button
            onClick={next}
            disabled={!hasNext}
            className="flex items-center gap-1 px-3 py-1.5 rounded-lg border border-border-subtle text-xs font-medium text-text-secondary hover:text-cei-blue disabled:opacity-40"
          >
            Next <ChevronRight size={12} aria-hidden="true" />
          </button>
        </nav>
      )}
    </div>
  );
}
