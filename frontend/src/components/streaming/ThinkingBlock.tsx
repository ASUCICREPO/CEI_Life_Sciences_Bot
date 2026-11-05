import { useEffect, useRef, useState } from 'react';
import { useSessionsClient } from '../../lib/useSessions';
import type { TraceEvent } from '../../lib/stream/events';
import type { StreamStatus } from '../../lib/stream/reducer';

// Collapsible "thinking" block (B6): live trace_events while the run streams; collapses when the
// stream ends; re-expanding a finished turn lazily loads the persisted trace from
// GET /sessions/{sessionId}/turns/{messageId}/trace (B59). The backend redacts traces (B7); this
// component additionally renders only allow-listed fields and drops never-show keys (defence in depth).

export type ThinkingBlockProps = {
  traceEvents: readonly TraceEvent[];
  status: StreamStatus;
  sessionId: string;
  messageId?: string;
  /** Injected for tests; defaults to the auth-bound sessions client. */
  loadTrace?: (sessionId: string, messageId: string) => Promise<readonly TraceEvent[]>;
};

type FetchState = { status: 'idle' | 'loading' | 'loaded' | 'error'; events: readonly TraceEvent[] };

const NEVER_SHOW_KEYS = new Set(['model_id', 'modelid', 'guardrail_config', 'guardrail_id', 'system_prompt', 'prompt_version', 'api_key', 'secret']);
// Mirrors backend/agents/shared/streaming/trace_redaction.py DENY_PATTERNS (subset that can appear in text).
const DENY_PATTERNS = [
  /\b(?:us|eu|apac|global)\.(?:anthropic|amazon|meta|cohere|mistral|ai21|deepseek)\./i,
  /\banthropic\.claude/i,
  /arn:aws:(?:bedrock|secretsmanager):/i,
  /guardrail/i,
  /system[\s_-]*prompt/i,
  /prompt[\s_-]*version/i,
  /\bAKIA[0-9A-Z]{16}\b/,
  /\bsk-[A-Za-z0-9_-]{8,}/,
  /\bBearer\s+/i,
];
const REDACTED = '[redacted]';

function safeText(value: string | undefined): string | undefined {
  if (value === undefined) return undefined;
  return DENY_PATTERNS.some((pattern) => pattern.test(value)) ? REDACTED : value;
}

function safeValue(value: unknown, depth = 0): unknown {
  if (typeof value === 'string') return safeText(value);
  if (value === null || typeof value !== 'object') return value;
  if (depth >= 8) return undefined;
  if (Array.isArray(value)) return value.map((item) => safeValue(item, depth + 1));
  return Object.fromEntries(
    Object.entries(value)
      .filter(([key]) => !NEVER_SHOW_KEYS.has(key.toLowerCase()) && safeText(key) === key)
      .map(([key, item]) => [key, safeValue(item, depth + 1)]),
  );
}

function isTerminal(status: StreamStatus): boolean {
  return status !== 'idle' && status !== 'streaming';
}

function stepTitle(event: TraceEvent): string {
  switch (event.step_type) {
    case 'plan': return 'Plan';
    case 'tool_call': return event.tool_name ? `Tool · ${safeText(event.tool_name)}` : 'Tool call';
    case 'source_access': return 'Source';
    case 'grounding_decision': return 'Grounding';
    case 'reasoning': return event.reasoning_label === 'full' ? 'Reasoning' : 'Reasoning (summarized)';
  }
}

export function ThinkingBlock({ traceEvents, status, sessionId, messageId, loadTrace }: ThinkingBlockProps) {
  const client = useSessionsClient();
  const terminal = isTerminal(status);
  const [open, setOpen] = useState(!terminal);
  const [fetched, setFetched] = useState<FetchState>({ status: 'idle', events: [] });
  const [previousStatus, setPreviousStatus] = useState(status);
  const requested = useRef(false);
  // Latest loader in a ref: a new client/loader identity must not re-trigger (or cancel) the fetch.
  const loader = useRef(loadTrace ?? (async (sid: string, mid: string) => (await client.loadTurnTrace(sid, mid)).events));
  loader.current = loadTrace ?? (async (sid: string, mid: string) => (await client.loadTurnTrace(sid, mid)).events);

  // Collapse once when the stream ends; the user can re-expand. Adjusted during render (not in an
  // effect) so the end-of-stream commit never sees open && terminal and fires the lazy fetch early.
  if (previousStatus !== status) {
    setPreviousStatus(status);
    if (!isTerminal(previousStatus) && terminal) setOpen(false);
  }

  // Lazy: the persisted trace is fetched only when a finished turn is expanded (B59), at most once.
  useEffect(() => {
    if (!open || !terminal || !messageId || requested.current) return;
    requested.current = true;
    let cancelled = false;
    let settled = false;
    setFetched({ status: 'loading', events: [] });
    loader.current(sessionId, messageId)
      .then((events) => { settled = true; if (!cancelled) setFetched({ status: 'loaded', events }); })
      .catch(() => { settled = true; if (!cancelled) setFetched({ status: 'error', events: [] }); });
    return () => {
      cancelled = true;
      // collapsed / unmounted mid-flight: let a later expand retry
      if (!settled) requested.current = false;
    };
  }, [open, terminal, messageId, sessionId]);

  if (traceEvents.length === 0 && !messageId) return null;
  const events = fetched.status === 'loaded' ? fetched.events : traceEvents;

  return (
    <section className="rounded-lg border border-border-subtle bg-surface-panel" aria-label="Thinking" data-testid="thinking-block">
      <button
        type="button"
        className="flex w-full items-center justify-between px-3 py-2 text-xs font-medium text-text-secondary"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <span>{terminal ? 'Show reasoning and tool calls' : 'Thinking…'}</span>
        <span aria-hidden="true">{open ? '−' : '+'}</span>
      </button>
      {open && (
        <div className="border-t border-border-subtle px-3 py-2">
          {fetched.status === 'loading' && <p className="text-xs text-text-tertiary" role="status">Loading trace…</p>}
          {fetched.status === 'error' && <p className="text-xs text-text-tertiary" role="status">Could not load the saved trace.</p>}
          <ol className="space-y-2 text-xs text-text-primary">
            {events.map((event, index) => (
              <TraceRow key={`${event.seq}-${index}`} event={event} />
            ))}
          </ol>
        </div>
      )}
    </section>
  );
}

function TraceRow({ event }: { event: TraceEvent }) {
  const text = safeText(event.reasoning_text ?? event.tool_output_summary);
  const input = event.tool_input ? safeValue(event.tool_input) : undefined;
  const sources = (event.sources ?? []).map((source) => safeText(source) ?? '').filter(Boolean);
  return (
    <li className="space-y-0.5" data-step-type={event.step_type}>
      <p className="font-medium text-text-secondary">{stepTitle(event)}</p>
      {text && <p className="whitespace-pre-wrap">{text}</p>}
      {input !== undefined && Object.keys(input as object).length > 0 && (
        <code className="block overflow-x-auto text-text-tertiary">{JSON.stringify(input)}</code>
      )}
      {sources.length > 0 && <p className="text-text-tertiary">Sources: {sources.join(', ')}</p>}
    </li>
  );
}
