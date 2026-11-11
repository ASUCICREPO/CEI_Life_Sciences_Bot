import { useEffect, useReducer, useRef, useState } from 'react';
import { CEIUISurface } from '../cei-ui/cei-ui-surface';
import { useAuth } from '../../lib/auth/AuthProvider';
import type { AgentEnvelope, AgentName } from '../../lib/agentClient';
import { openStream as defaultOpenStream, StreamError, type OpenStreamOptions } from '../../lib/stream/openStream';
import { initialStreamState, reduceEvent, selectTree, type StreamInteraction, type StreamState } from '../../lib/stream/reducer';
import type { StreamEvent } from '../../lib/stream/events';
import { ThinkingBlock } from './ThinkingBlock';

/** Called when a stream `hitl_request` card is answered; `seq` identifies which interaction. */
export type StreamInteractionAction = (seq: number, envelope: AgentEnvelope) => void;

export type StreamingSurfaceProps = {
  sessionId: string;
  agent: AgentName;
  /** Request body for POST /chat/stream (prompt, agent, mode, ...). */
  body: Record<string, unknown>;
  /** Stream URL; defaults to the same-origin CloudFront streaming route. */
  url?: string;
  /** The AI turn's message id, once known (enables the lazy trace fetch after the stream ends). */
  messageId?: string;
  onStateChange?: (state: StreamState) => void;
  /** Called on a terminal protocol event or unrecoverable connection failure. */
  onFinished?: (state: StreamState, connectionError: string | null) => void;
  /** Injected for tests. */
  openStreamImpl?: (options: OpenStreamOptions) => AsyncIterable<StreamEvent>;
  /** Seq numbers of hitl interactions already answered (disables their card). */
  answeredInteractions?: ReadonlySet<number>;
  /** Called when a hitl interaction card is answered via POST /ui-actions. */
  onInteractionAction?: StreamInteractionAction;
};

const PROGRESS_LABEL: Record<NonNullable<StreamState['progress']>['status'], string> = {
  planning: 'Planning',
  running: 'Working',
  verifying: 'Verifying',
  finalizing: 'Finalizing',
};

export function StreamingSurface({ sessionId, agent, body, url, messageId, onStateChange, onFinished, openStreamImpl, answeredInteractions, onInteractionAction }: StreamingSurfaceProps) {
  const { getAccessToken } = useAuth();
  const [state, dispatch] = useReducer(reduceEvent, initialStreamState);
  const [connectionError, setConnectionError] = useState<string | null>(null);
  const streamUrl = url ?? '/chat/stream';
  const bodyKey = JSON.stringify(body);
  // Latest token getter / opener in refs: a new identity must not abort and re-open a live stream.
  const tokenRef = useRef(getAccessToken);
  tokenRef.current = getAccessToken;
  const openRef = useRef(openStreamImpl ?? defaultOpenStream);
  openRef.current = openStreamImpl ?? defaultOpenStream;
  const finishedRef = useRef(false);
  const finishedCallback = useRef(onFinished);
  finishedCallback.current = onFinished;

  useEffect(() => {
    const controller = new AbortController();
    setConnectionError(null);
    (async () => {
      try {
        const stream = openRef.current({
          url: streamUrl,
          body: JSON.parse(bodyKey),
          getToken: () => tokenRef.current(),
          signal: controller.signal,
        });
        for await (const event of stream) {
          if (controller.signal.aborted) return;
          dispatch(event);
        }
      } catch (error) {
        if (controller.signal.aborted) return;
        setConnectionError(error instanceof StreamError ? error.message : 'The response stream failed.');
      }
    })();
    return () => controller.abort();
  }, [streamUrl, bodyKey]);

  useEffect(() => {
    onStateChange?.(state);
  }, [state, onStateChange]);

  useEffect(() => {
    if (finishedRef.current || (!connectionError && (state.status === 'idle' || state.status === 'streaming'))) return;
    finishedRef.current = true;
    finishedCallback.current?.(state, connectionError);
  }, [state, connectionError]);

  return (
    <StreamView
      state={state}
      connectionError={connectionError}
      sessionId={sessionId}
      agent={agent}
      messageId={messageId}
      answeredInteractions={answeredInteractions}
      onInteractionAction={onInteractionAction}
    />
  );
}

/** Render a saved stream state without opening another network request. */
export function StreamView({ state, connectionError, sessionId, agent, messageId, answeredInteractions, onInteractionAction }: {
  state: StreamState;
  connectionError?: string | null;
  sessionId: string;
  agent: AgentName;
  messageId?: string;
  answeredInteractions?: ReadonlySet<number>;
  onInteractionAction?: StreamInteractionAction;
}) {
  const tree = selectTree(state);
  const pendingGrounding = Object.values(state.grounding).filter((detail) => detail.status === 'pending').length;
  const streaming = state.status === 'streaming' || state.status === 'idle';

  return (
    <div className="streaming-surface space-y-3" aria-busy={streaming} data-status={state.status} data-message-id={messageId}>
      {state.progress && streaming && (
        <p className="text-xs text-text-secondary" role="status">
          {PROGRESS_LABEL[state.progress.status]}
          {state.progress.step_label ? ` — ${state.progress.step_label}` : ''}
          {state.progress.current_step !== undefined && state.progress.total_steps
            ? ` (${state.progress.current_step}/${state.progress.total_steps})`
            : ''}
        </p>
      )}
      <ThinkingBlock traceEvents={state.traceEvents} status={state.status} sessionId={sessionId} messageId={messageId ?? state.messageId ?? undefined} />
      {tree && <CEIUISurface surface={tree} agent={agent} sessionId={sessionId} disabled={streaming} />}
      {pendingGrounding > 0 && (
        <p className="text-xs text-text-tertiary" role="status" data-testid="grounding-pending">
          Verifying citations…
        </p>
      )}
      {state.interactions.map((interaction) => (
        <InteractionCard
          key={interaction.seq}
          interaction={interaction}
          agent={agent}
          sessionId={sessionId}
          answered={answeredInteractions?.has(interaction.seq) ?? false}
          onAction={onInteractionAction}
        />
      ))}
      {state.error && (
        <p className="rounded-md border border-border-subtle p-3 text-sm text-text-primary" role="alert">
          {state.error.message}
        </p>
      )}
      {connectionError && !state.error && (
        <p className="rounded-md border border-border-subtle p-3 text-sm text-text-primary" role="alert">
          {connectionError}
        </p>
      )}
    </div>
  );
}

// ADR: hitl_request cards post through /ui-actions, like the buffered path | job_handoff
// stays render-only here (no polling); the chat navigates to /pipeline on the resumed envelope's job_id.
function InteractionCard({ interaction, agent, sessionId, answered, onAction }: {
  interaction: StreamInteraction;
  agent: AgentName;
  sessionId: string;
  answered: boolean;
  onAction?: StreamInteractionAction;
}) {
  if (interaction.kind === 'hitl') {
    return (
      <section className="rounded-lg border border-border-subtle bg-surface-elevated p-3 space-y-2" aria-label="Waiting for you">
        <p className="text-sm font-medium text-text-primary">{answered ? 'Answered' : 'Waiting for you'}</p>
        <CEIUISurface
          surface={interaction.surface}
          agent={agent}
          sessionId={sessionId}
          disabled={answered}
          onAction={onAction ? (envelope) => onAction(interaction.seq, envelope) : undefined}
        />
      </section>
    );
  }
  return (
    <section className="rounded-lg border border-border-subtle bg-surface-elevated p-3" aria-label="Background job">
      <p className="text-sm font-medium text-text-primary">This run moved to a background job.</p>
      <p className="text-xs text-text-secondary">Job {interaction.job_id}. The answer will appear in this chat when it finishes.</p>
    </section>
  );
}
