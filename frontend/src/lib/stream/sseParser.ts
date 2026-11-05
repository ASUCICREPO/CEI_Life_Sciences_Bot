// Incremental SSE (text/event-stream) parser for fetch + ReadableStream (B4: no EventSource,
// because EventSource cannot send the Authorization header the Cognito authorizer needs).
// Frames are separated by a blank line; `data:` lines are joined with "\n"; lines starting
// with ":" are comments (the protocol's `:heartbeat` keep-alive).

export type SseFrame = {
  id?: string;
  event?: string;
  data: string;
  /** Comment text of a comment-only frame (e.g. "heartbeat"). */
  comment?: string;
};

export class SseParser {
  private buffer = '';
  private frame: { id?: string; event?: string; data: string[]; comments: string[] } = { data: [], comments: [] };

  /** Feed decoded text; returns every frame completed by this chunk. */
  push(chunk: string): SseFrame[] {
    this.buffer += chunk;
    const frames: SseFrame[] = [];
    let match: RegExpExecArray | null;
    const lineBreak = /\r\n|\r|\n/g;
    let consumed = 0;
    while ((match = lineBreak.exec(this.buffer)) !== null) {
      // a trailing lone "\r" may be the first half of "\r\n": wait for more input
      if (match[0] === '\r' && match.index === this.buffer.length - 1) break;
      const line = this.buffer.slice(consumed, match.index);
      consumed = match.index + match[0].length;
      const frame = this.line(line);
      if (frame) frames.push(frame);
    }
    this.buffer = this.buffer.slice(consumed);
    return frames;
  }

  /** End of stream: a frame without its terminating blank line is discarded (per the SSE spec). */
  flush(): SseFrame[] {
    this.buffer = '';
    this.frame = { data: [], comments: [] };
    return [];
  }

  private line(line: string): SseFrame | null {
    if (line === '') return this.dispatch();
    if (line.startsWith(':')) {
      this.frame.comments.push(line.slice(1).trim());
      return null;
    }
    const colon = line.indexOf(':');
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? '' : line.slice(colon + 1);
    if (value.startsWith(' ')) value = value.slice(1);
    if (field === 'data') this.frame.data.push(value);
    else if (field === 'event') this.frame.event = value;
    else if (field === 'id' && !value.includes('\u0000')) this.frame.id = value;
    // `retry:` and unknown fields are ignored: reconnect is driven by openStream, not the browser
    return null;
  }

  private dispatch(): SseFrame | null {
    const { id, event, data, comments } = this.frame;
    this.frame = { data: [], comments: [] };
    if (data.length > 0) return { id, event, data: data.join('\n') };
    if (comments.length > 0 && event === undefined) return { data: '', comment: comments.join('\n') };
    return null;
  }
}

/**
 * Fold one SSE frame into the raw event object parseStreamEvent expects:
 * `{ seq: Number(id), type: event, ...data }`; a `:heartbeat` comment becomes `{ type: 'heartbeat', ts }`.
 * Returns null for frames that are not protocol events (other comments, undecodable data).
 */
export function frameToRawEvent(frame: SseFrame, now: () => string = () => new Date().toISOString()): unknown {
  if (frame.comment !== undefined) return frame.comment === 'heartbeat' ? { type: 'heartbeat', ts: now() } : null;
  let data: unknown;
  try {
    data = JSON.parse(frame.data);
  } catch {
    return null;
  }
  if (typeof data !== 'object' || data === null || Array.isArray(data)) return null;
  const seq = frame.id !== undefined && /^\d+$/.test(frame.id) ? Number(frame.id) : (data as { seq?: unknown }).seq;
  return { ...(data as Record<string, unknown>), seq, type: frame.event ?? (data as { type?: unknown }).type };
}
