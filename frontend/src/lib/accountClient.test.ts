import { describe, expect, it, vi } from 'vitest';
import { createAccountClient, AccountClientError } from './accountClient';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

describe('createAccountClient', () => {
  it('never calls the API without an access token', async () => {
    const fetchImpl = vi.fn();
    const client = createAccountClient(async () => null, { fetchImpl });
    await expect(client.deleteAccount()).rejects.toMatchObject({ status: 401, code: 'unauthorized' });
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it('POSTs an empty body with the bearer token and returns the deleting status on 202', async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({ status: 'deleting' }, 202));
    const client = createAccountClient(async () => 'tok-123', { fetchImpl, baseUrl: 'https://api.example/v1/' });
    const result = await client.deleteAccount();
    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('https://api.example/v1/account/delete');
    expect(init.method).toBe('POST');
    expect((init.headers as Record<string, string>).authorization).toBe('Bearer tok-123');
    expect(JSON.parse(init.body as string)).toEqual({});
    expect(result).toEqual({ status: 'deleting' });
  });

  it('returns 202 again on a repeat call', async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({ status: 'deleting' }, 202));
    const client = createAccountClient(async () => 'tok', { fetchImpl });
    await expect(client.deleteAccount()).resolves.toEqual({ status: 'deleting' });
    await expect(client.deleteAccount()).resolves.toEqual({ status: 'deleting' });
    expect(fetchImpl).toHaveBeenCalledTimes(2);
  });

  it('maps the api.md error envelope to AccountClientError', async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse({ error: { code: 'internal_error', message: 'Something went wrong. Please try again.', retriable: true, request_id: 'r1' } }, 500),
    );
    const client = createAccountClient(async () => 'tok', { fetchImpl });
    const error = await client.deleteAccount().catch((e: unknown) => e);
    expect(error).toBeInstanceOf(AccountClientError);
    expect(error).toMatchObject({
      status: 500,
      code: 'internal_error',
      message: 'Something went wrong. Please try again.',
      requestId: 'r1',
      retriable: true,
    });
  });

  it('rejects a response without a status string', async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({ nope: true }, 202));
    const client = createAccountClient(async () => 'tok', { fetchImpl });
    await expect(client.deleteAccount()).rejects.toBeInstanceOf(AccountClientError);
  });
});
