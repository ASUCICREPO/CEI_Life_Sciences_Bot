import { describe, expect, it } from 'vitest';
import { SseParser, frameToRawEvent } from './sseParser';
import { openStream, StreamError } from './openStream';
import { parseStreamEvent, type StreamEvent } from './events';

function sse(seq: number, type: string, data: Record<string, unknown>): string {
  return `id: ${seq}\nevent: ${type}\ndata: ${JSON.stringify(data)}\n\n`;
}

const BEGIN = sse(0, 'surface_begin', { surface_id: 's', revision: 1, version: '1.2', root_id: 'root' });
const ROOT = sse(1, 'component_add', { parent_id: null, component: { id: 'root', type: 'Column', children: [] } });
const END = sse(2, 'surface_end', { surface_id: 's', revision: 1, root_id: 'root', final: true });

function streamOf(chunks: string[], { fail = false } = {}): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  let index = 0;
  return new ReadableStream({
    pull(controller) {
      if (index < chunks.length) controller.enqueue(encoder.encode(chunks[index++]));
      else if (fail) controller.error(new TypeError('network error'));
      else controller.close();
    },
  });
}

type Call = { url: string; init: RequestInit };

function fakeFetch(responses: (() => Response)[]) {
  const calls: Call[] = [];
  const impl = (async (url: string, init: RequestInit) => {
    calls.push({ url, init });
    const next = responses.shift();
    if (!next) throw new TypeError('no more responses');
    return next();
  }) as unknown as typeof fetch;
  return { calls, impl };
}

async function collect(iterable: AsyncIterable<StreamEvent>): Promise<StreamEvent[]> {
  const out: StreamEvent[] = [];
  for await (const event of iterable) out.push(event);
  return out;
}

const noSleep = async () => undefined;

describe('SseParser', () => {
  it('assembles frames across arbitrary chunk boundaries and CRLF', () => {
    const parser = new SseParser();
    const text = 'id: 3\r\nevent: usage\r\ndata: {"input_tokens":1,\r\ndata: "output_tokens":2}\r\n\r\n';
    const frames = [...text].flatMap((char) => parser.push(char));
    expect(frames).toEqual([{ id: '3', event: 'usage', data: '{"input_tokens":1,\n"output_tokens":2}' }]);
  });

  it('turns a :heartbeat comment into a seq-less heartbeat event', () => {
    const [frame] = new SseParser().push(':heartbeat\n\n');
    expect(frameToRawEvent(frame, () => 'T')).toEqual({ type: 'heartbeat', ts: 'T' });
  });

  it('folds id and event into the event object', () => {
    const [frame] = new SseParser().push(sse(7, 'progress', { status: 'running' }));
    expect(parseStreamEvent(frameToRawEvent(frame))).toEqual({ seq: 7, type: 'progress', status: 'running' });
  });

  it('drops an unterminated trailing frame and undecodable data', () => {
    const parser = new SseParser();
    expect(parser.push('id: 1\nevent: x\ndata: {')).toEqual([]);
    expect(parser.flush()).toEqual([]);
    const [frame] = new SseParser().push('event: x\ndata: not json\n\n');
    expect(frameToRawEvent(frame)).toBeNull();
  });
});

describe('openStream', () => {
  it('POSTs with the bearer token over fetch (not EventSource) and stops at the terminal event', async () => {
    const { calls, impl } = fakeFetch([() => new Response(streamOf([BEGIN + ROOT, END, sse(3, 'usage', { input_tokens: 1, output_tokens: 1 })]))]);
    const events = await collect(openStream({ url: '/api/chat/stream', body: { prompt: 'hi' }, getToken: async () => 'tok', fetchImpl: impl }));
    expect(events.map((event) => event.type)).toEqual(['surface_begin', 'component_add', 'surface_end']);
    expect(calls).toHaveLength(1);
    const headers = calls[0].init.headers as Record<string, string>;
    expect(calls[0].init.method).toBe('POST');
    expect(headers.Authorization).toBe('Bearer tok');
    expect(headers.Accept).toBe('text/event-stream');
    expect(JSON.parse(calls[0].init.body as string)).toEqual({ prompt: 'hi' });
  });

  it('reconnects after a drop with resume_from_seq = last seq (B68)', async () => {
    const { calls, impl } = fakeFetch([
      () => new Response(streamOf([BEGIN, ROOT], { fail: true })),
      () => new Response(streamOf([ROOT, END])), // server replays from the log; duplicates are still yielded
    ]);
    const events = await collect(openStream({ url: '/s', body: { prompt: 'hi' }, getToken: async () => 'tok', fetchImpl: impl, sleep: noSleep }));
    expect(calls).toHaveLength(2);
    expect(JSON.parse(calls[1].init.body as string)).toEqual({ prompt: 'hi', resume_from_seq: 1 });
    expect(events.map((event) => event.type)).toEqual(['surface_begin', 'component_add', 'component_add', 'surface_end']);
  });

  it('does not resubmit a request that disconnected before its first event', async () => {
    const delays: number[] = [];
    const { calls, impl } = fakeFetch(Array.from({ length: 5 }, () => () => new Response('x', { status: 503 })));
    await expect(collect(openStream({
      url: '/s', body: {}, getToken: async () => 'tok', fetchImpl: impl, maxReconnects: 2,
      sleep: async (ms) => { delays.push(ms); },
    }))).rejects.toBeInstanceOf(StreamError);
    expect(calls).toHaveLength(1);
    expect(delays).toEqual([]);
  });

  it('surfaces the safe API error message for unavailable background processing', async () => {
    const { impl } = fakeFetch([() => new Response(JSON.stringify({ error: { code: 'service_unavailable', message: 'Background processing is not available yet.' } }), { status: 400 })]);
    await expect(collect(openStream({ url: '/s', body: {}, getToken: async () => 'tok', fetchImpl: impl })))
      .rejects.toMatchObject({ status: 400, message: 'Background processing is not available yet.' });
  });

  it('shows the server concurrency-limit message without starting a second run', async () => {
    const { calls, impl } = fakeFetch([() => new Response(JSON.stringify({ error: { code: 'concurrency_limit', message: 'Too many active runs.' } }), { status: 429 })]);
    await expect(collect(openStream({ url: '/s', body: {}, getToken: async () => 'tok', fetchImpl: impl })))
      .rejects.toMatchObject({ status: 429, message: 'Too many active runs.' });
    expect(calls).toHaveLength(1);
  });

  it('does not retry a 4xx and requires a token', async () => {
    const { calls, impl } = fakeFetch([() => new Response('x', { status: 403 })]);
    await expect(collect(openStream({ url: '/s', body: {}, getToken: async () => 'tok', fetchImpl: impl }))).rejects.toMatchObject({ status: 403 });
    expect(calls).toHaveLength(1);
    await expect(collect(openStream({ url: '/s', body: {}, getToken: async () => null, fetchImpl: impl }))).rejects.toMatchObject({ status: 401 });
  });

  it('stops quietly when aborted', async () => {
    const controller = new AbortController();
    const { impl } = fakeFetch([() => new Response(streamOf([BEGIN], { fail: true }))]);
    const events: StreamEvent[] = [];
    for await (const event of openStream({ url: '/s', body: {}, getToken: async () => 'tok', fetchImpl: impl, signal: controller.signal, sleep: noSleep })) {
      events.push(event);
      controller.abort();
    }
    expect(events).toHaveLength(1);
  });
});
