import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useApp } from '../context/AppContext';
import { resumeState, useSessions } from '../lib/useSessions';
import {
  ArrowRight, Clock, CheckCircle2, AlertCircle,
  FileText, FlaskConical, Briefcase, Sparkles, Send
} from 'lucide-react';
import { ViewMode } from '../design-system/tokens';

export function HomePage() {
  const { mode } = useApp();
  const navigate = useNavigate();
  const { sessions, isLoading: sessionsLoading, error: sessionsError } = useSessions({ limit: 5 });
  const [askValue, setAskValue] = useState('');

  // F9: free text starts a classified chat on /ask instead of a deterministic task route.
  const handleAsk = (event: React.FormEvent) => {
    event.preventDefault();
    const trimmed = askValue.trim();
    if (!trimmed) return;
    navigate('/ask', { state: { prompt: trimmed } });
  };

  const statusIcon = {
    active: <Clock size={14} className="text-cei-blue-light" />,
    archived: <CheckCircle2 size={14} className="text-success" />,
  };

  const quickActions = mode === ViewMode.SCIENCE
    ? [
        { label: 'Research a question', path: '/tasks', icon: FlaskConical },
        { label: 'Check a sequence', path: '/tasks', icon: FileText },
        { label: 'Validate a target', path: '/tasks', icon: Sparkles },
      ]
    : [
        { label: 'Regulatory pathway', path: '/tasks', icon: Briefcase },
        { label: 'Patent landscape', path: '/tasks', icon: FileText },
        { label: 'Market sizing', path: '/tasks', icon: Sparkles },
      ];

  return (
    <div className="max-w-6xl mx-auto px-6 py-8">
      <div data-tour="home-overview" className="mb-8">
        <h1 className="text-2xl font-semibold text-text-primary">Good morning, Sarah</h1>
        <p className="text-sm text-text-secondary mt-1">Home is your overview for starting work and returning to recent conversations.</p>
      </div>

      <form onSubmit={handleAsk} className="mb-8">
        <div className="relative">
          <input
            type="text"
            value={askValue}
            onChange={(event) => setAskValue(event.target.value)}
            placeholder="Ask anything, research, drafting, analysis, or a multi-step plan..."
            className="w-full pl-4 pr-12 py-3 rounded-xl border border-border-subtle bg-surface-elevated text-sm placeholder:text-text-tertiary focus:border-cei-blue-light focus:ring-2 focus:ring-cei-blue-light/20 transition-all shadow-sm"
            aria-label="Ask Founder AI"
          />
          <button
            type="submit"
            disabled={!askValue.trim()}
            className="absolute right-2 top-1/2 -translate-y-1/2 p-2 rounded-lg bg-cei-blue text-white disabled:opacity-30 disabled:cursor-not-allowed hover:bg-cei-navy transition-colors"
            aria-label="Send"
          >
            <Send size={14} />
          </button>
        </div>
        <p className="text-xs text-text-tertiary mt-1.5 px-1">Founder AI picks the best-fit specialist automatically, or asks which one you want.</p>
      </form>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-3 mb-8">
        {quickActions.map((action) => {
          const Icon = action.icon;
          return (
            <button
              key={action.label}
              onClick={() => navigate(action.path)}
              className="flex items-center gap-3 px-4 py-3 bg-surface-elevated border border-border-subtle rounded-lg hover:border-cei-blue-light/40 hover:shadow-sm transition-all text-left group"
            >
              <div className="w-9 h-9 rounded-lg bg-cei-blue/5 flex items-center justify-center group-hover:bg-cei-blue/10 transition-colors">
                <Icon size={18} className="text-cei-blue" />
              </div>
              <span className="flex-1 text-sm font-medium text-text-primary">{action.label}</span>
              <ArrowRight size={14} className="text-text-tertiary group-hover:text-cei-blue-light transition-colors" />
            </button>
          );
        })}
      </div>

      <div className="flex items-center justify-between mb-3">
        <h2 className="text-sm font-semibold text-text-primary">Recent chats</h2>
        <button onClick={() => navigate('/history')} className="text-xs text-cei-blue-light hover:text-cei-blue font-medium">View all</button>
      </div>

      <div className="space-y-2">
        {sessionsLoading && <p className="text-xs text-text-tertiary px-1 py-2">Loading your chats…</p>}
        {!sessionsLoading && sessionsError && (
          <p role="alert" className="flex items-center gap-2 text-xs text-text-secondary px-1 py-2">
            <AlertCircle size={14} className="text-evidence-weak" aria-hidden="true" />
            {sessionsError}
          </p>
        )}
        {!sessionsLoading && !sessionsError && sessions.length === 0 && (
          <p className="text-xs text-text-tertiary px-1 py-2">No chats yet. Start a task to begin.</p>
        )}
        {sessions.map((session) => (
          <button
            key={session.session_id}
            onClick={() => navigate('/tasks', { state: resumeState(session) })}
            className="w-full text-left px-4 py-3 bg-surface-elevated border border-border-subtle rounded-lg hover:border-cei-blue-light/30 hover:shadow-sm transition-all group"
          >
            <div className="flex items-start gap-3">
              <div className="mt-0.5">{statusIcon[session.status] ?? statusIcon.active}</div>
              <div className="flex-1 min-w-0">
                <p className="text-sm font-medium text-text-primary group-hover:text-cei-blue transition-colors truncate">{session.title || 'Untitled chat'}</p>
                <p className="text-xs text-text-secondary mt-0.5 line-clamp-1">
                  {session.message_count} {session.message_count === 1 ? 'message' : 'messages'}{session.agent_type ? ` · ${session.agent_type}` : ''}
                </p>
                <div className="flex items-center gap-3 mt-1.5">
                  {session.mode && <span className="text-[10px] text-text-tertiary uppercase tracking-wide">{session.mode}</span>}
                  {session.updated_at && <span className="text-[10px] text-text-tertiary">Updated {new Date(session.updated_at).toLocaleDateString()}</span>}
                </div>
              </div>
              <ArrowRight size={14} className="text-text-tertiary group-hover:text-cei-blue-light mt-1 shrink-0 transition-colors" />
            </div>
          </button>
        ))}
      </div>

      <div className="mt-8 px-4 py-2.5 bg-amber-50/50 border border-amber-200/40 rounded-lg">
        <p className="text-xs text-amber-700"><span className="font-semibold">Recent chats are your real sessions.</span> Quick actions open the task launcher.</p>
      </div>
    </div>
  );
}
