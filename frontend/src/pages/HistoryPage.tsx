import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { AlertCircle, ArrowRight, Briefcase, FlaskConical, Loader2, MessageSquare } from 'lucide-react';
import { resumeState, useSessions } from '../lib/useSessions';
import type { SessionSort, SessionsClient } from '../lib/sessionsClient';

interface HistoryPageProps {
  /** Injectable for tests; defaults to a client bound to the signed-in user's token. */
  client?: SessionsClient;
}

export function HistoryPage({ client }: HistoryPageProps = {}) {
  const navigate = useNavigate();
  const [sort, setSort] = useState<SessionSort>('updated');
  const { sessions, isLoading, isLoadingMore, error, hasMore, loadMore, reload } = useSessions({ sort, client });

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
            className="px-2 py-1 rounded-md border border-border-subtle bg-surface-elevated text-xs text-text-primary"
          >
            <option value="updated">Last updated</option>
            <option value="created">Date created</option>
          </select>
        </label>
      </div>

      {isLoading && (
        <div role="status" className="flex items-center gap-2 text-sm text-text-secondary py-8 justify-center">
          <Loader2 size={16} className="animate-spin text-cei-blue" aria-hidden="true" />
          <span>Loading your chats…</span>
        </div>
      )}

      {!isLoading && error && (
        <div role="alert" className="flex items-center gap-3 p-4 rounded-xl border border-evidence-weak/40 bg-surface-elevated">
          <AlertCircle size={16} className="text-evidence-weak shrink-0" aria-hidden="true" />
          <p className="flex-1 text-sm text-text-primary">{error}</p>
          <button onClick={reload} className="text-xs font-medium text-cei-blue-light hover:text-cei-blue">Retry</button>
        </div>
      )}

      {!isLoading && !error && sessions.length === 0 && (
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
            const when = sort === 'created' ? session.created_at : session.updated_at;
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
                    {when && (
                      <p className="text-xs text-text-tertiary mt-2">
                        {sort === 'created' ? 'Created' : 'Last message'} {new Date(when).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })}
                      </p>
                    )}
                  </div>
                  <ArrowRight size={15} className="text-text-tertiary group-hover:text-cei-blue-light mt-1 transition-colors" />
                </div>
              </button>
            );
          })}
          {hasMore && (
            <div className="flex justify-center pt-2">
              <button
                onClick={loadMore}
                disabled={isLoadingMore}
                className="flex items-center gap-2 px-4 py-2 rounded-lg border border-border-subtle text-xs font-medium text-text-secondary hover:text-cei-blue disabled:opacity-60"
              >
                {isLoadingMore && <Loader2 size={12} className="animate-spin" aria-hidden="true" />}
                {isLoadingMore ? 'Loading…' : 'Load older chats'}
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
