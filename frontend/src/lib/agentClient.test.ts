import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { buildRunPayload, getOrCreateSessionId, invokeAgent, setAgentTokenProvider } from './agentClient';
import { loadRuntimeConfig, resetRuntimeConfigForTests } from './auth/config';

describe('buildRunPayload', () => {
  const input = { prompt: 'p', mode: 'science' as const, analysisType: 'market' as const, documentType: 'grant' as const };

  it('sends document_type only to drafting', () => {
    expect(buildRunPayload('drafting', input)).toEqual({ prompt: 'p', mode: 'science', document_type: 'grant' });
  });

  it('sends analysis_type only to analysis', () => {
    expect(buildRunPayload('analysis', input)).toEqual({ prompt: 'p', mode: 'science', analysis_type: 'market' });
  });

  it('sends neither to research or pipeline', () => {
    for (const agent of ['research', 'pipeline'] as const) {
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

  it('creates plain UUID session ids that match the backend session-id contract', () => {
    const id = getOrCreateSessionId('thread-a');
    expect(id).toMatch(/^[A-Za-z0-9_-]{1,64}$/);
    expect(id.startsWith('cei-demo-')).toBe(false);
    expect(getOrCreateSessionId('thread-a')).toBe(id);
  });
});
