import { describe, expect, it, vi } from 'vitest';
import { createSessionsClient, SessionsClientError, toHistoryMessage } from './sessionsClient';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

describe('createSessionsClient', () => {
  it('never calls the API without an access token', async () => {
    const fetchImpl = vi.fn();
    const client = createSessionsClient(async () => null, { fetchImpl });
    await expect(client.listSessions()).rejects.toMatchObject({ status: 401, code: 'unauthorized' });
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it('sends the bearer token and the sort/limit/cursor query, and never a user_id', async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({ items: [], next_cursor: null }));
    const client = createSessionsClient(async () => 'tok-123', { fetchImpl, baseUrl: 'https://api.example/v1/' });
    await client.listSessions({ sort: 'created', limit: 5, cursor: 'abc' });
    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('https://api.example/v1/sessions?sort=created&limit=5&cursor=abc');
    expect((init.headers as Record<string, string>).authorization).toBe('Bearer tok-123');
    expect(url).not.toContain('user_id');
  });

  it('maps the api.md error envelope to SessionsClientError', async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse({ error: { code: 'not_found', message: 'Chat not found.', retriable: false, request_id: 'r1' } }, 404),
    );
    const client = createSessionsClient(async () => 'tok', { fetchImpl });
    const error = await client.getSession('s1').catch((e: unknown) => e);
    expect(error).toBeInstanceOf(SessionsClientError);
    expect(error).toMatchObject({ status: 404, code: 'not_found', message: 'Chat not found.', requestId: 'r1', retriable: false });
  });

  it('treats 5xx as retriable', async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({}, 503));
    const client = createSessionsClient(async () => 'tok', { fetchImpl });
    await expect(client.listSessions()).rejects.toMatchObject({ status: 503, retriable: true });
  });

  it('url-encodes the session id and returns the history cursor + session ref', async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse({
        items: [{ message_id: 'm1', role: 'human', sequence_number: 0, content_blocks: [{ block_type: 'text', body: 'hi' }] }],
        next_cursor: 'older',
        session: { session_id: 's/1', title: 'T', title_source: 'generated', agent_type: 'research', mode: 'science' },
      }),
    );
    const client = createSessionsClient(async () => 'tok', { fetchImpl });
    const page = await client.loadHistory('s/1', { limit: 50 });
    expect((fetchImpl.mock.calls[0] as unknown as [string])[0]).toBe('/api/sessions/s%2F1/history?limit=50');
    expect(page.has_more).toBe(true);
    expect(page.next_cursor).toBe('older');
    expect(page.session).toMatchObject({ session_id: 's/1', title_source: 'generated', agent_type: 'research', mode: 'science' });
    expect(page.items[0].text).toBe('hi');
  });

  it('rejects a response without an items array', async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({ nope: true }));
    const client = createSessionsClient(async () => 'tok', { fetchImpl });
    await expect(client.listSessions()).rejects.toBeInstanceOf(SessionsClientError);
  });
});

describe('toHistoryMessage', () => {
  it('joins text blocks and flags an invalid ui block instead of rendering it', () => {
    const message = toHistoryMessage({
      message_id: 'm2',
      role: 'ai',
      sequence_number: 3,
      content_blocks: [
        { block_type: 'text', body: 'a' },
        { block_type: 'ui', body: { not: 'a surface' } },
        { block_type: 'text', body: 'b' },
      ],
    });
    expect(message.role).toBe('ai');
    expect(message.text).toBe('a\n\nb');
    expect(message.ui).toBeUndefined();
    expect(message.ui_error).toBeTruthy();
  });

  it('defaults unknown roles to human', () => {
    expect(toHistoryMessage({ role: 'system' }).role).toBe('human');
  });
});
