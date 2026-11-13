import { TERMINAL_EVENT_TYPES, parseStreamEvent, type StreamEvent } from './events';
import { SseParser, frameToRawEvent } from './sseParser';
import { reportClientFailure } from '../clientDiagnostics';

// Opens POST /chat/stream over fetch + ReadableStream (B4) and yields typed stream events.
// On a dropped connection it re-opens the stream with backoff, sending `resume_from_seq`
// (the last seq it yielded) in the request body — the decided B68 resume shape in
// .agents/specs/streaming-protocol.md. Replayed duplicates are still yielded; the reducer's
// seq guard discards them, so this layer stays stateless about the surface.

export type TokenGetter = () => Promise<string | null>;

export type OpenStreamOptions = {
  url: string;
  body: Record<string, unknown>;
  getToken: TokenGetter;
  signal?: AbortSignal;
  fetchImpl?: typeof fetch;
  /** Reconnect attempts after a drop without progress (default 3). */
  maxReconnects?: number;
  /** Delay before reconnect attempt `n` (1-based). Default: 500 ms * 2^(n-1), capped at 8 s. */
  backoffMs?: (attempt: number) => number;
  sleep?: (ms: number, signal?: AbortSignal) => Promise<void>;
  now?: () => string;
};

export class StreamError extends Error {
  status?: number;
  retriable: boolean;
  constructor(message: string, details: { status?: number; retriable?: boolean; cause?: unknown } = {}) {
    super(message, { cause: details.cause });
    this.name = 'StreamError';
    this.status = details.status;
    this.retriable = details.retriable ?? false;
  }
}

const defaultBackoff = (attempt: number) => Math.min(500 * 2 ** (attempt - 1), 8_000);

function defaultSleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, ms);
    signal?.addEventListener('abort', () => {
      clearTimeout(timer);
      resolve();
    }, { once: true });
  });
}

type Outcome = 'terminal' | 'dropped';

export async function* openStream(options: OpenStreamOptions): AsyncGenerator<StreamEvent, void, undefined> {
  const fetchImpl = options.fetchImpl ?? ((input: RequestInfo | URL, init?: RequestInit) => fetch(input, init));
  const maxReconnects = options.maxReconnects ?? 3;
  const backoff = options.backoffMs ?? defaultBackoff;
  const sleep = options.sleep ?? defaultSleep;
  let lastSeq = -1;
  let attempt = 0;
  let invalidReported = false;

  while (!options.signal?.aborted) {
    let token: string | null;
    try { token = await options.getToken(); }
    catch (error) { reportClientFailure('auth', 'auth_error'); throw error; }
    if (!token) throw new StreamError('You are signed out. Sign in again to continue.', { status: 401 });
    const body = lastSeq >= 0 ? { ...options.body, resume_from_seq: lastSeq } : options.body;

    let outcome: Outcome = 'dropped';
    let progressed = false;
    try {
      const response = await fetchImpl(options.url, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${token}`,
          Accept: 'text/event-stream',
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(body),
        signal: options.signal,
      });
      if (!response.ok || !response.body) {
        if (response.ok && !response.body) reportClientFailure('stream', 'invalid_response');
        const retriable = response.status >= 500 || response.status === 429;
        if (!retriable || lastSeq < 0 || response.status === 429) {
          const body: unknown = await response.json().catch(() => null);
          const envelope = body && typeof body === 'object' && 'error' in body ? (body as { error?: unknown }).error : null;
          const requestId = envelope && typeof envelope === 'object' && 'request_id' in envelope && typeof envelope.request_id === 'string'
            ? envelope.request_id : undefined;
          if (response.status >= 500 || response.status === 429) reportClientFailure('stream', 'http_error', response.status, requestId);
          const message = envelope && typeof envelope === 'object' && 'message' in envelope
            && typeof envelope.message === 'string' && envelope.message.length <= 1000
            ? envelope.message : `The stream could not be opened (HTTP ${response.status}).`;
          throw new StreamError(message, { status: response.status, retriable });
        }
        reportClientFailure('stream', 'http_error', response.status);
      } else {
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        const parser = new SseParser();
        try {
          reading: while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            for (const frame of parser.push(decoder.decode(value, { stream: true }))) {
              const raw = frameToRawEvent(frame, options.now);
              const event = parseStreamEvent(raw);
              if (!event) {
                if (frame.comment === undefined && !invalidReported) {
                  invalidReported = true;
                  reportClientFailure('stream', 'invalid_response');
                }
                continue;
              }
              if (event.type !== 'heartbeat') {
                if (event.seq > lastSeq) {
                  lastSeq = event.seq;
                  progressed = true;
                }
              }
              yield event;
              if (TERMINAL_EVENT_TYPES.has(event.type)) {
                outcome = 'terminal';
                break reading;
              }
            }
          }
        } finally {
          parser.flush();
          await reader.cancel().catch(() => undefined);
        }
      }
    } catch (error) {
      if (options.signal?.aborted) return;
      if (error instanceof StreamError) throw error;
      // network failure mid-request or mid-read: treated as a drop
    }

    if (outcome === 'terminal' || options.signal?.aborted) return;
    // A request may have reached the server even if no event made it back. Without a replay
    // sequence there is no safe way to distinguish it from a new run; do not submit it again.
    if (lastSeq < 0) {
      reportClientFailure('stream', 'stream_dropped');
      throw new StreamError('The connection was lost before the response began. Check the chat history before retrying.', { retriable: true });
    }
    attempt = progressed ? 1 : attempt + 1;
    if (attempt > maxReconnects) {
      reportClientFailure('stream', 'stream_dropped');
      throw new StreamError('The connection was lost and could not be resumed.', { retriable: true });
    }
    await sleep(backoff(attempt), options.signal);
  }
}

// ADR: a one-shot consumer for an already-opened SSE Response | used for a POST /ui-actions response that
// came back as text/event-stream (a drafting outline approval starting the full draft) instead of JSON.
// Unlike openStream above, there is nothing to resume: the request already happened exactly once.
export async function* consumeEventStream(
  response: Response,
  now: () => string = () => new Date().toISOString(),
): AsyncGenerator<StreamEvent, void, undefined> {
  if (!response.body) return;
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const parser = new SseParser();
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      for (const frame of parser.push(decoder.decode(value, { stream: true }))) {
        const raw = frameToRawEvent(frame, now);
        const event = parseStreamEvent(raw);
        if (!event) continue;
        yield event;
        if (TERMINAL_EVENT_TYPES.has(event.type)) return;
      }
    }
  } finally {
    parser.flush();
    await reader.cancel().catch(() => undefined);
  }
}
