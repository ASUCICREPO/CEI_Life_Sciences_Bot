import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useVirtualizer } from '@tanstack/react-virtual';
import { Bot, Loader2, Send, User } from 'lucide-react';
import { CEIUISurface } from './cei-ui/cei-ui-surface';
import { SafeMarkdown } from './cei-ui/markdown';
import {
  AgentClientError,
  buildRunPayload,
  invokeAgent,
  setJobSession,
  startNewSessionId,
  type AgentEnvelope,
  type AgentMode,
  type AgentName,
  type AnalysisType,
  type DocumentType,
} from '../lib/agentClient';
import type { CEIUISurface as CEIUISurfaceType } from '../lib/cei-ui/schema';
import { SessionsClientError, type HistoryMessage } from '../lib/sessionsClient';
import { useSessionsClient } from '../lib/useSessions';
import { useModelsClient } from '../lib/useModels';
import type { ModelOption } from '../lib/modelsClient';
import { useAuth } from '../lib/auth/AuthProvider';
import { StreamingSurface, StreamView } from './streaming/StreamingSurface';
import { ThinkingBlock } from './streaming/ThinkingBlock';
import { ReportExports } from './ReportExports';
import type { StreamState } from '../lib/stream/reducer';
import { agentForAgentType } from '../lib/resumeRouting';

interface LiveMessage {
  id: string;
  role: 'user' | 'assistant';
  text?: string;
  surface?: CEIUISurfaceType;
  uiError?: string;
  answered?: boolean;
  isError?: boolean;
  historyMessageId?: string;
  stream?: { state: StreamState; connectionError: string | null };
  /** Seq numbers of this stream's hitl_request interactions already answered via /ui-actions. */
  answeredInteractions?: ReadonlySet<number>;
  /** F9: set when the envelope that produced this turn names the agent the router picked. */
  routedAgent?: AgentName;
}

// F9: the router's ChoicePicker (session-runner/index.js, ui-actions/index.js) always uses this
// fixed interaction id; its resumed envelope's action_response.value is the chosen agent string.
const ROUTER_CHOICE_INTERACTION_ID = 'router-choice';

const AGENT_LABELS: Readonly<Record<AgentName, string>> = {
  research: 'Research',
  drafting: 'Drafting',
  analysis: 'Analysis',
  pipeline: 'Pipeline Manager',
};

interface LiveAgentChatProps {
  /** Known agent for a dashboard task; absent starts a free-text turn the Session Router classifies (F9). */
  agent?: AgentName;
  mode: AgentMode;
  analysisType?: AnalysisType;
  /** Drafting only: the document type every turn carries (the runtime requires it). */
  documentType?: DocumentType;
  threadKey: string;
  /** Existing backend session to continue (e.g. opened from History); its history is loaded on mount. */
  resumeSessionId?: string;
  title: string;
  description: string;
  examples: string[];
  placeholder: string;
  /** Sent once on mount (e.g. a prompt typed on Home before navigating here). */
  autoSendPrompt?: string;
}

const HISTORY_PAGE_SIZE = 30;
const LOAD_OLDER_THRESHOLD_PX = 200;

// History turns are rendered read-only: tool rows are internal, and past HITL surfaces are not re-answerable.
function toLiveMessages(items: HistoryMessage[]): LiveMessage[] {
  return items
    .filter((item) => item.role !== 'tool')
    .map((item): LiveMessage => ({
      id: `history-${item.message_id || item.sequence_number}`,
      role: item.role === 'human' ? 'user' : 'assistant',
      text: item.text,
      surface: item.ui,
      uiError: item.ui_error,
      answered: true,
      historyMessageId: item.role === 'ai' ? item.message_id : undefined,
    }));
}

// ADR: a dedicated live chat for the Research, Drafting, Analysis and Pipeline tasks | the mock task
// chat (ChatResponse/taskConversations) stays untouched; this component talks
// to the AgentCore runtimes through agentClient and reads history via sessionsClient.
export function LiveAgentChat({ agent, mode, analysisType, documentType, threadKey, resumeSessionId, title, description, examples, placeholder, autoSendPrompt }: LiveAgentChatProps) {
  const navigate = useNavigate();
  const sessionsClient = useSessionsClient();
  const modelsClient = useModelsClient();
  const { config } = useAuth();
  const streamingAvailable = config?.researchStreamingEnabled === true;
  // F9: a free-text turn (no agent prop) is always classified by the Session Router through /chat/stream
  // (Drafting/Analysis/Pipeline Manager arrive as adapted SSE); an explicit task page keeps Research-only
  // streaming unchanged (api.md, F09 "Implementation notes").
  const useChatStream = streamingAvailable && (agent === 'research' || agent === undefined);
  const routerMode = agent === undefined;
  const routerDisabled = routerMode && !streamingAvailable;
  // ADR: fallback label for CEI UI dispatch before an agent is known | the owning agent for /ui-actions
  // comes from the session's own stored agent_type, never from this value (ui-actions/index.js), so a
  // placeholder is safe until a router pick or a classified response names the real one.
  const effectiveAgent: AgentName = agent ?? 'research';
  // One session per mounted chat: a resumed chat continues its own session, anything else starts a new one.
  // Lazy state so the id is fixed for this chat's lifetime; the parent remounts via `key`.
  const [sessionId, setSessionId] = useState(() => resumeSessionId ?? startNewSessionId(threadKey));
  const createdSessionId = useRef<string | undefined>(resumeSessionId);
  const [activeStream, setActiveStream] = useState<{ sessionId: string; body: Record<string, unknown> } | null>(null);
  const [messages, setMessages] = useState<LiveMessage[]>([]);
  const [inputValue, setInputValue] = useState('');
  const [isRunning, setIsRunning] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(Boolean(resumeSessionId));
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [olderCursor, setOlderCursor] = useState<string | null>(null);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [showReports, setShowReports] = useState(false);
  // The task-view toggle sets the initial mode; an active chat owns its own selection.
  const [chatMode, setChatMode] = useState<AgentMode>(mode);
  const [modeSaving, setModeSaving] = useState(false);
  const [modeError, setModeError] = useState<string | null>(null);
  // '' means "use the account/session default" (no override picked yet).
  const [models, setModels] = useState<ModelOption[]>([]);
  const [chatModel, setChatModel] = useState('');
  const formId = useId();

  // Loaded once per client identity; a failed load just hides the picker (every agent still works).
  useEffect(() => {
    let cancelled = false;
    modelsClient.listModels().then((list) => {
      if (!cancelled) setModels(list);
    }).catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [modelsClient]);

  const scrollRef = useRef<HTMLDivElement | null>(null);
  // Set right before prepending older messages so the layout effect can keep the viewport anchored.
  const prependAnchor = useRef<{ scrollTop: number; totalSize: number } | null>(null);
  const lastMessageId = useRef<string | null>(null);

  const virtualizer = useVirtualizer({
    count: messages.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => 120,
    overscan: 3,
    getItemKey: (index) => messages[index]?.id ?? index,
  });

  // Newest history page first (items in a page are chronological); `next_cursor` points to older messages.
  useEffect(() => {
    if (!resumeSessionId) return;
    let cancelled = false;
    setHistoryLoading(true);
    setHistoryError(null);
    sessionsClient
      .loadHistory(resumeSessionId, { limit: HISTORY_PAGE_SIZE })
      .then((page) => {
        if (cancelled) return;
        const loaded = toLiveMessages(page.items);
        // The latest assistant surface may still be awaiting input, so keep it answerable.
        const last = loaded[loaded.length - 1];
        if (last && last.role === 'assistant' && last.surface) last.answered = false;
        setMessages(loaded);
        setOlderCursor(page.next_cursor);
        if (page.session.mode === 'science' || page.session.mode === 'business') setChatMode(page.session.mode);
        if (page.session.model_id) setChatModel(page.session.model_id);
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setHistoryError(error instanceof SessionsClientError ? error.message : 'Could not load this conversation.');
      })
      .finally(() => {
        if (!cancelled) setHistoryLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [resumeSessionId, sessionsClient]);

  const loadOlder = useCallback(() => {
    if (!resumeSessionId || !olderCursor || loadingOlder) return;
    setLoadingOlder(true);
    sessionsClient
      .loadHistory(resumeSessionId, { limit: HISTORY_PAGE_SIZE, cursor: olderCursor })
      .then((page) => {
        const older = toLiveMessages(page.items);
        const element = scrollRef.current;
        if (element && older.length > 0) {
          prependAnchor.current = { scrollTop: element.scrollTop, totalSize: virtualizer.getTotalSize() };
        }
        setMessages((prev) => {
          const known = new Set(prev.map((message) => message.id));
          return [...older.filter((message) => !known.has(message.id)), ...prev];
        });
        setOlderCursor(page.next_cursor);
      })
      .catch((error: unknown) => {
        setHistoryError(error instanceof SessionsClientError ? error.message : 'Could not load older messages.');
      })
      .finally(() => setLoadingOlder(false));
  }, [resumeSessionId, olderCursor, loadingOlder, sessionsClient, virtualizer]);

  // Preserve the viewport when older messages are prepended; follow the conversation when a new turn is appended.
  useLayoutEffect(() => {
    const element = scrollRef.current;
    const anchor = prependAnchor.current;
    if (element && anchor) {
      prependAnchor.current = null;
      element.scrollTop = anchor.scrollTop + (virtualizer.getTotalSize() - anchor.totalSize);
      return;
    }
    const newest = messages[messages.length - 1]?.id ?? null;
    if (newest && newest !== lastMessageId.current) {
      virtualizer.scrollToIndex(messages.length - 1, { align: 'end' });
    }
    lastMessageId.current = newest;
  }, [messages, virtualizer]);

  const handleScroll = () => {
    const element = scrollRef.current;
    if (element && element.scrollTop < LOAD_OLDER_THRESHOLD_PX) loadOlder();
  };

  // F9: a router-choice resume carries no agent run on `rejected`/`stale` (expired pick or a surface
  // that moved on); show its message like any other failed action instead of an empty bubble.
  function appendFromEnvelope(envelope: AgentEnvelope, isError = false): string {
    const failed = envelope.status === 'rejected' || envelope.status === 'stale';
    const id = `${agent ?? 'router'}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const actionResponse = envelope.action_response;
    const routedValue = actionResponse && actionResponse.interaction_id === ROUTER_CHOICE_INTERACTION_ID
      ? actionResponse.value : undefined;
    setMessages((prev) => [...prev, {
      id,
      role: 'assistant',
      text: envelope.text || (failed ? envelope.action_response?.message ?? undefined : undefined),
      surface: envelope.ui,
      uiError: envelope.ui_error,
      isError: isError || failed,
      routedAgent: typeof routedValue === 'string' ? agentForAgentType(routedValue) ?? undefined : undefined,
    }]);
    if (envelope.job_id) {
      setJobSession(envelope.job_id, sessionId);
      navigate(`/pipeline?job=${encodeURIComponent(envelope.job_id)}`);
    }
    return id;
  }

  async function changeChatMode(next: AgentMode) {
    if (next === chatMode || modeSaving || isRunning || historyLoading || historyError) return;
    setModeError(null);
    const existingSession = createdSessionId.current;
    if (!existingSession) {
      setChatMode(next);
      return;
    }
    setModeSaving(true);
    try {
      await sessionsClient.updateSession(existingSession, { mode: next });
      setChatMode(next);
    } catch {
      setModeError('Could not update this chat’s mode. Please try again.');
    } finally {
      setModeSaving(false);
    }
  }

  // ADR: the model is fixed once the chat exists | api.md: PATCH /sessions accepts only title and mode; F08: a
  // session uses the model it was created with for every turn
  const modelLocked = Boolean(resumeSessionId) || messages.length > 0;

  function changeChatModel(next: string) {
    if (modelLocked || next === chatModel || isRunning) return;
    setChatModel(next);
  }

  async function askQuestion(prompt: string) {
    const trimmed = prompt.trim();
    if (!trimmed || isRunning || modeSaving || historyError || routerDisabled) return;
    setMessages((prev) => [...prev, { id: `user-${Date.now()}`, role: 'user', text: trimmed }]);
    setInputValue('');
    setIsRunning(true);
    let streamStarted = false;
    try {
      if (useChatStream) {
        let currentSessionId = createdSessionId.current;
        if (!currentSessionId) {
          const created = await sessionsClient.createSession({
            title: trimmed.slice(0, 100),
            mode: chatMode,
            ...(chatModel ? { model_id: chatModel } : {}),
          });
          currentSessionId = created.session_id;
          createdSessionId.current = currentSessionId;
          setSessionId(currentSessionId);
        }
        const body: Record<string, unknown> = { session_id: currentSessionId, run_id: crypto.randomUUID(), message: trimmed, mode: chatMode };
        // F9: an explicit task page keeps its deterministic task_type; free text (no agent prop) omits it
        // so the Session Router classifies the message instead.
        if (agent) body.task_type = threadKey;
        setActiveStream({ sessionId: currentSessionId, body });
        streamStarted = true;
        return;
      }
      if (!agent) return; // routerDisabled already blocks this when streaming is unavailable
      const payload = buildRunPayload(agent, { prompt: trimmed, mode: chatMode, analysisType, documentType });
      const envelope = await invokeAgent(agent, payload, sessionId);
      createdSessionId.current = sessionId;
      appendFromEnvelope(envelope);
    } catch (error) {
      const message = error instanceof AgentClientError || error instanceof SessionsClientError ? error.message : 'The agent could not be reached. Try again in a moment.';
      setMessages((prev) => [...prev, { id: `error-${Date.now()}`, role: 'assistant', text: message, isError: true }]);
    } finally {
      // Streaming completion owns the running state; setup failures must still unlock the form.
      if (!streamStarted) setIsRunning(false);
    }
  }

  const finishStream = useCallback((state: StreamState, connectionError: string | null) => {
    setMessages((prev) => [...prev, {
      id: `stream-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      role: 'assistant',
      stream: { state, connectionError },
    }]);
    setActiveStream(null);
    setIsRunning(false);
  }, []);

  function handleAction(messageId: string, envelope: AgentEnvelope) {
    // ADR: disable the answered gate, then append the resumed surface as a new
    // assistant turn | keeps the HITL history visible instead of overwriting it.
    setMessages((prev) => prev.map((message) => (message.id === messageId ? { ...message, answered: true } : message)));
    appendFromEnvelope(envelope);
  }

  // Mirrors handleAction for a stream's hitl_request card: mark that one interaction answered
  // (a stream can carry several), then append the resumed surface as a new assistant turn.
  function handleStreamInteractionAction(messageId: string, seq: number, envelope: AgentEnvelope) {
    setMessages((prev) => prev.map((message) => (message.id === messageId
      ? { ...message, answeredInteractions: new Set([...(message.answeredInteractions ?? []), seq]) }
      : message)));
    appendFromEnvelope(envelope);
  }

  const handleSend = (event: React.FormEvent) => {
    event.preventDefault();
    void askQuestion(inputValue);
  };

  // Latest askQuestion in a ref: an effect dependency on the function itself would redefine and
  // re-fire on every render (it is not memoized, same reasoning as the token/opener refs above).
  const askQuestionRef = useRef(askQuestion);
  askQuestionRef.current = askQuestion;
  const autoSentRef = useRef(false);
  useEffect(() => {
    if (!autoSendPrompt || autoSentRef.current || historyLoading) return;
    autoSentRef.current = true;
    void askQuestionRef.current(autoSendPrompt);
  }, [autoSendPrompt, historyLoading]);

  const renderMessage = (message: LiveMessage) => (
    <>
      {message.role === 'user' && (
        <div className="flex items-start gap-3 max-w-2xl">
          <div className="w-6 h-6 rounded-full bg-cei-blue/10 flex items-center justify-center flex-shrink-0 mt-0.5">
            <User size={12} className="text-cei-blue" />
          </div>
          <p className="text-sm text-text-primary leading-relaxed">{message.text}</p>
        </div>
      )}
      {message.role === 'assistant' && (
        <div className="flex items-start gap-3">
          <div className={`w-6 h-6 rounded-full flex items-center justify-center flex-shrink-0 mt-0.5 ${message.isError ? 'bg-evidence-weak' : 'bg-cei-blue'}`}>
            <Bot size={12} className="text-white" />
          </div>
          <div className="flex-1 min-w-0 max-w-3xl">
            {message.uiError && (
              <p className="text-xs text-evidence-moderate mb-1.5">{message.uiError} Showing the plain-text answer instead.</p>
            )}
            {useChatStream && message.historyMessageId && (
              <ThinkingBlock traceEvents={[]} status="ended" sessionId={sessionId} messageId={message.historyMessageId} />
            )}
            {message.routedAgent && (
              <p className="text-xs font-medium text-cei-blue mb-1.5">Routed to {AGENT_LABELS[message.routedAgent]}</p>
            )}
            {message.stream ? (
              <StreamView
                state={message.stream.state}
                connectionError={message.stream.connectionError}
                sessionId={sessionId}
                agent={effectiveAgent}
                answeredInteractions={message.answeredInteractions}
                onInteractionAction={(seq, envelope) => handleStreamInteractionAction(message.id, seq, envelope)}
              />
            ) : message.surface ? (
              <CEIUISurface
                surface={message.surface}
                agent={message.routedAgent ?? effectiveAgent}
                sessionId={sessionId}
                fallbackText={message.text}
                disabled={message.answered}
                onAction={(envelope) => handleAction(message.id, envelope)}
              />
            ) : (
              <SafeMarkdown content={message.text ?? ''} />
            )}
          </div>
        </div>
      )}
    </>
  );

  const showEmptyState = messages.length === 0 && !historyLoading && !historyError;

  return (
    <div className="h-full flex flex-col">
      <div className="border-b border-border-subtle px-6 py-3 bg-surface-elevated flex-shrink-0">
        <div className="flex items-center justify-between gap-4">
          <p className="text-sm font-semibold text-text-primary">{title}</p>
          <div className="flex items-center gap-3">
            <label className="flex items-center gap-1.5 text-xs text-text-secondary">
              <span>Chat mode</span>
              <select aria-label="Chat mode" value={chatMode} disabled={isRunning || modeSaving || historyLoading || Boolean(historyError)}
                onChange={(event) => void changeChatMode(event.target.value as AgentMode)}
                className="rounded border border-border-subtle bg-surface-elevated px-2 py-1 text-text-primary disabled:opacity-50">
                <option value="science">Science</option>
                <option value="business">Business</option>
              </select>
            </label>
            {models.length > 0 && (
              <label className="flex items-center gap-1.5 text-xs text-text-secondary">
                <span>Model</span>
                <select aria-label="Chat model" value={chatModel}
                  disabled={modelLocked || isRunning || modeSaving || historyLoading || Boolean(historyError)}
                  title={modelLocked ? 'The model is fixed when the chat starts.' : undefined}
                  onChange={(event) => void changeChatModel(event.target.value)}
                  className="rounded border border-border-subtle bg-surface-elevated px-2 py-1 text-text-primary disabled:opacity-50">
                  <option value="">Default</option>
                  {models.map((model) => <option key={model.model_id} value={model.model_id}>{model.label}</option>)}
                </select>
              </label>
            )}
            {agent === 'drafting' && (resumeSessionId || messages.length > 0) && (
            <button type="button" aria-expanded={showReports} onClick={() => setShowReports((open) => !open)}
              className="text-xs font-medium text-cei-blue hover:underline">
              {showReports ? 'Hide reports' : 'Reports'}
            </button>
            )}
          </div>
        </div>
        <p className="text-xs text-text-secondary mt-0.5">{description}</p>
        {modeError && <p role="alert" className="mt-1 text-xs text-evidence-weak">{modeError}</p>}
      </div>

      {showReports && <ReportExports key={sessionId} sessionId={sessionId} />}

      <div ref={scrollRef} onScroll={handleScroll} className="flex-1 overflow-y-auto px-6 py-6">
        {historyLoading && (
          <div role="status" className="flex items-center justify-center gap-2 py-6 text-sm text-text-secondary">
            <Loader2 size={16} className="animate-spin text-cei-blue" aria-hidden="true" />
            <span>Loading conversation…</span>
          </div>
        )}
        {historyError && (
          <p role="alert" className="text-xs text-evidence-weak text-center py-2">{historyError}</p>
        )}
        {loadingOlder && (
          <div className="flex items-center justify-center gap-2 py-2 text-xs text-text-tertiary">
            <Loader2 size={12} className="animate-spin" aria-hidden="true" />
            <span>Loading older messages…</span>
          </div>
        )}

        {showEmptyState && (
          <div className="h-full flex flex-col items-center justify-center text-center max-w-md mx-auto">
            <div className="w-12 h-12 rounded-2xl bg-cei-blue/10 flex items-center justify-center mb-4">
              <Bot size={24} className="text-cei-blue" />
            </div>
            <h2 className="text-base font-semibold text-text-primary">Describe what you need</h2>
            <p className="text-sm text-text-secondary mt-1.5">{description}</p>
            <div className="flex flex-wrap gap-2 justify-center mt-4">
              {examples.map((example) => (
                <button
                  key={example}
                  onClick={() => void askQuestion(example)}
                  className="text-xs px-3 py-1.5 rounded-full border border-border-subtle text-text-secondary hover:border-cei-blue-light/40 hover:text-cei-blue transition-all"
                >
                  {example}
                </button>
              ))}
            </div>
          </div>
        )}

        {messages.length > 0 && (
          <div className="relative w-full" style={{ height: virtualizer.getTotalSize() }}>
            {virtualizer.getVirtualItems().map((item) => {
              const message = messages[item.index];
              return (
                <div
                  key={item.key}
                  data-index={item.index}
                  ref={virtualizer.measureElement}
                  className="absolute top-0 left-0 w-full pb-5"
                  style={{ transform: `translateY(${item.start}px)` }}
                >
                  {renderMessage(message)}
                </div>
              );
            })}
          </div>
        )}

        {activeStream && (
          <div className="flex items-start gap-3 pb-5">
            <div className="w-6 h-6 rounded-full bg-cei-blue flex items-center justify-center flex-shrink-0 mt-0.5">
              <Bot size={12} className="text-white" />
            </div>
            <div className="flex-1 min-w-0 max-w-3xl">
              <StreamingSurface sessionId={activeStream.sessionId} agent={effectiveAgent} body={activeStream.body} onFinished={finishStream} />
            </div>
          </div>
        )}
        {isRunning && !activeStream && (
          <div className="flex items-start gap-3">
            <div className="w-6 h-6 rounded-full bg-cei-blue flex items-center justify-center flex-shrink-0 mt-0.5">
              <Bot size={12} className="text-white" />
            </div>
            <div className="flex items-center gap-1.5 px-3 py-2 rounded-lg bg-surface-panel">
              <span className="w-1.5 h-1.5 rounded-full bg-cei-blue/60 animate-pulse" />
              <span className="w-1.5 h-1.5 rounded-full bg-cei-blue/60 animate-pulse [animation-delay:150ms]" />
              <span className="w-1.5 h-1.5 rounded-full bg-cei-blue/60 animate-pulse [animation-delay:300ms]" />
              <span className="text-xs text-text-tertiary ml-1">Working. This can take a few minutes...</span>
            </div>
          </div>
        )}
      </div>

      <div className="border-t border-border-subtle bg-surface-elevated px-6 py-4 flex-shrink-0">
        {routerDisabled && (
          <p role="status" className="max-w-2xl mx-auto mb-2 text-xs text-text-tertiary">
            Free-text routing is not available in this environment yet.
          </p>
        )}
        <form id={formId} onSubmit={handleSend} className="max-w-2xl mx-auto">
          <div className="relative">
            <textarea
              value={inputValue}
              onChange={(event) => setInputValue(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && !event.shiftKey) {
                  event.preventDefault();
                  handleSend(event);
                }
              }}
              rows={1}
              disabled={isRunning || historyLoading || modeSaving || Boolean(historyError) || routerDisabled}
              placeholder={placeholder}
              className="w-full resize-y pl-4 pr-12 py-3 rounded-xl border border-border-subtle bg-surface-panel text-sm placeholder:text-text-tertiary focus:border-cei-blue-light focus:ring-2 focus:ring-cei-blue-light/20 transition-all disabled:opacity-60"
              aria-label={placeholder}
            />
            <button
              type="submit"
              disabled={!inputValue.trim() || isRunning || historyLoading || modeSaving || Boolean(historyError) || routerDisabled}
              className="absolute right-2 top-2.5 p-2 rounded-lg bg-cei-blue text-white disabled:opacity-30 disabled:cursor-not-allowed hover:bg-cei-navy transition-colors"
              aria-label="Send"
            >
              <Send size={14} />
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
