import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  buildRunPayload,
  getOrCreateSessionId,
  invokeAgent,
  isSessionUuid,
  sendAction,
  sendActionExpectingStream,
  setAgentTokenProvider,
  startNewSessionId,
} from './agentClient';
import { loadRuntimeConfig, resetRuntimeConfigForTests } from './auth/config';

function sse(seq: number, type: string, data: Record<string, unknown>): string {
  return `id: ${seq}\nevent: ${type}\ndata: ${JSON.stringify(data)}\n\n`;
}

function streamOf(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  let index = 0;
  return new ReadableStream({
    pull(controller) {
      if (index < chunks.length) controller.enqueue(encoder.encode(chunks[index++]));
      else controller.close();
    },
  });
}

describe('buildRunPayload', () => {
  const input = { prompt: 'p', mode: 'science' as const, analysisType: 'market' as const };

  it('sends analysis_type only to analysis', () => {
    expect(buildRunPayload('analysis', input)).toEqual({ prompt: 'p', mode: 'science', analysis_type: 'market' });
  });

  it('never sends document_type: the backend infers the document type itself', () => {
    for (const agent of ['research', 'drafting', 'pipeline'] as const) {
      expect(Object.keys(buildRunPayload(agent, input)).sort()).toEqual(['mode', 'prompt']);
    }
  });

  it('omits absent turn context', () => {
    expect(Object.keys(buildRunPayload('drafting', { prompt: 'p', mode: 'business' })).sort()).toEqual(['mode', 'prompt']);
  });
});

describe('agentClient', () => {
  beforeEach(() => {
    sessionStorage.clear();
    resetRuntimeConfigForTests();
  });
  afterEach(() => {
    setAgentTokenProvider(null);
    resetRuntimeConfigForTests();
  });

  it('refuses to invoke an agent without an access token (no request is sent)', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    setAgentTokenProvider(async () => null);
    await expect(invokeAgent('research', { prompt: 'x', mode: 'science' }, 's1')).rejects.toThrow(/signed out/i);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('refuses when no token provider is registered', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    await expect(invokeAgent('research', { prompt: 'x', mode: 'science' }, 's1')).rejects.toThrow(/signed out/i);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('sends the bearer token and runtime session header, and no user_id in the body', async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ text: 'ok', status: 'completed' }), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);
    setAgentTokenProvider(async () => 'tok-abc');
    const envelope = await invokeAgent('drafting', { prompt: 'hello', mode: 'business' }, 'sess-1');
    expect(envelope.text).toBe('ok');
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('/api/agents/drafting');
    const headers = init.headers as Record<string, string>;
    expect(headers.authorization).toBe('Bearer tok-abc');
    expect(headers['X-Amzn-Bedrock-AgentCore-Runtime-Session-Id']).toBe('sess-1');
    const body = JSON.parse(String(init.body));
    expect(body).toEqual({ op: 'run', prompt: 'hello', mode: 'business' });
    expect(body).not.toHaveProperty('user_id');
  });

  it('calls the agent proxy URL from config.json agentUrls with the same auth and session headers', async () => {
    const draftingUrl = 'https://abc123xyz.lambda-url.us-east-1.on.aws/drafting';
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            region: 'us-east-1',
            userPoolId: 'us-east-1_abc',
            userPoolClientId: 'client123',
            cognitoDomain: 'https://cei-ls-1.auth.us-east-1.amazoncognito.com',
            agentUrls: { drafting: draftingUrl },
          }),
          { status: 200 },
        ),
      ),
    );
    await loadRuntimeConfig();

    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ text: 'ok', status: 'completed' }), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);
    setAgentTokenProvider(async () => 'tok-abc');
    await invokeAgent('drafting', { prompt: 'hello', mode: 'business' }, 'sess-1');
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(draftingUrl);
    const headers = init.headers as Record<string, string>;
    expect(headers.authorization).toBe('Bearer tok-abc');
    expect(headers['X-Amzn-Bedrock-AgentCore-Runtime-Session-Id']).toBe('sess-1');

    // Agents without a configured URL keep the relative dev-proxy path
    await invokeAgent('research', { prompt: 'hello', mode: 'business' }, 'sess-1');
    expect((fetchMock.mock.calls[1] as unknown as [string])[0]).toBe('/api/agents/research');
  });

  const ACTION = {
    request_id: 'r1', session_id: 'sess-1', surface_id: 'analysis-scope', revision: 1,
    interaction_id: 'i-1', component_id: 'c-1', action_id: 'approve-scope', value: null,
  };

  it('sends actions to POST /ui-actions without the AgentCore session header when an API is configured', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
      region: 'us-east-1', userPoolId: 'us-east-1_abc', userPoolClientId: 'client123',
      cognitoDomain: 'https://cei-ls-1.auth.us-east-1.amazoncognito.com',
      apiBaseUrl: 'https://abc.execute-api.us-east-1.amazonaws.com/v1',
    }), { status: 200 })));
    await loadRuntimeConfig();
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ text: 'ok', status: 'completed' }), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);
    setAgentTokenProvider(async () => 'tok-abc');
    await sendAction('analysis', ACTION, 'sess-1');
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('https://abc.execute-api.us-east-1.amazonaws.com/v1/ui-actions');
    expect(JSON.parse(String(init.body))).toEqual(ACTION);
    const headers = init.headers as Record<string, string>;
    expect(headers.authorization).toBe('Bearer tok-abc');
    expect(headers).not.toHaveProperty('X-Amzn-Bedrock-AgentCore-Runtime-Session-Id');
  });

  it('keeps the agent proxy resume path without an API (local demo)', async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ text: 'ok', status: 'completed' }), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);
    setAgentTokenProvider(async () => 'tok-abc');
    await sendAction('analysis', ACTION, 'sess-1');
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('/api/agents/analysis');
    expect(JSON.parse(String(init.body))).toEqual({ op: 'resume', action: ACTION });
  });

  async function withApiBaseUrl() {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
      region: 'us-east-1', userPoolId: 'us-east-1_abc', userPoolClientId: 'client123',
      cognitoDomain: 'https://cei-ls-1.auth.us-east-1.amazoncognito.com',
      apiBaseUrl: 'https://abc.execute-api.us-east-1.amazonaws.com/v1',
    }), { status: 200 })));
    await loadRuntimeConfig();
  }

  describe('sendActionExpectingStream', () => {
    it('sends an SSE Accept header and consumes an event-stream response', async () => {
      await withApiBaseUrl();
      const fetchMock = vi.fn(async () => new Response(
        streamOf([sse(0, 'surface_begin', { surface_id: 's', root_id: 'root' }), sse(1, 'surface_end', {})]),
        { status: 200, headers: { 'content-type': 'text/event-stream' } },
      ));
      vi.stubGlobal('fetch', fetchMock);
      setAgentTokenProvider(async () => 'tok-abc');
      const result = await sendActionExpectingStream('drafting', ACTION, 'sess-1');
      expect(result.kind).toBe('stream');
      const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
      expect((init.headers as Record<string, string>).accept).toContain('text/event-stream');
      if (result.kind !== 'stream') throw new Error('expected a stream result');
      const types: string[] = [];
      for await (const event of result.events) types.push(event.type);
      expect(types).toEqual(['surface_begin', 'surface_end']);
    });

    it('returns the JSON envelope when the response is not an event stream', async () => {
      await withApiBaseUrl();
      const fetchMock = vi.fn(async () => new Response(JSON.stringify({ text: 'ok', status: 'completed' }), { status: 200 }));
      vi.stubGlobal('fetch', fetchMock);
      setAgentTokenProvider(async () => 'tok-abc');
      const result = await sendActionExpectingStream('drafting', ACTION, 'sess-1');
      expect(result.kind).toBe('envelope');
      if (result.kind !== 'envelope') throw new Error('expected an envelope result');
      expect(result.envelope.text).toBe('ok');
    });

    it('falls back to the agent proxy JSON envelope when no API is configured (local demo)', async () => {
      const fetchMock = vi.fn(async () => new Response(JSON.stringify({ text: 'ok', status: 'completed' }), { status: 200 }));
      vi.stubGlobal('fetch', fetchMock);
      setAgentTokenProvider(async () => 'tok-abc');
      const result = await sendActionExpectingStream('drafting', ACTION, 'sess-1');
      expect(result.kind).toBe('envelope');
      const [url] = fetchMock.mock.calls[0] as unknown as [string];
      expect(url).toBe('/api/agents/drafting');
    });
  });

  it('maps proxy 401 / 429 / 504 to actionable messages', async () => {
    setAgentTokenProvider(async () => 'tok-abc');
    const cases: Array<[number, RegExp]> = [
      [401, /sign-in has expired/],
      [429, /busy/],
      [504, /did not respond in time/],
      [500, /HTTP 500/],
    ];
    for (const [status, message] of cases) {
      vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', { status })));
      await expect(invokeAgent('research', { prompt: 'x', mode: 'science' }, 'sess-1')).rejects.toThrow(message);
    }
  });

  const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

  it('creates UUID session ids that match the backend session-id contract', () => {
    const id = getOrCreateSessionId('thread-a');
    expect(id).toMatch(UUID);
    expect(id.startsWith('cei-demo-')).toBe(false);
    expect(getOrCreateSessionId('thread-a')).toBe(id);
  });

  it('replaces a stored non-UUID (legacy or default) session id with a fresh UUID', () => {
    for (const legacy of ['session-1', 'default', 'cei-demo-123', '']) {
      sessionStorage.setItem('cei-session:thread-legacy', legacy);
      const id = getOrCreateSessionId('thread-legacy');
      expect(id).toMatch(UUID);
      expect(sessionStorage.getItem('cei-session:thread-legacy')).toBe(id);
    }
  });

  it('starts a distinct UUID session id for every new chat on the same thread', () => {
    const first = startNewSessionId('research');
    const second = startNewSessionId('research');
    expect(first).toMatch(UUID);
    expect(second).toMatch(UUID);
    expect(second).not.toBe(first);
    expect(sessionStorage.getItem('cei-session:research')).toBe(second);
  });

  it('isSessionUuid accepts only 8-4-4-4-12 hex ids', () => {
    expect(isSessionUuid(crypto.randomUUID())).toBe(true);
    for (const bad of ['default', 'session-1', '', null, undefined, '{12345678-1234-1234-1234-123456789abc}']) {
      expect(isSessionUuid(bad)).toBe(false);
    }
  });
});
