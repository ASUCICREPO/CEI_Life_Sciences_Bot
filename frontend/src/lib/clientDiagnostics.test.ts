import { afterEach, describe, expect, it, vi } from 'vitest';
import { configureClientDiagnostics, installGlobalDiagnostics, reportClientFailure } from './clientDiagnostics';

afterEach(() => { configureClientDiagnostics(null); });

describe('browser diagnostics', () => {
  it('sends only fixed fields and a validated request id', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    configureClientDiagnostics({ baseUrl: 'https://api.example/v1/', getToken: async () => 'private-token', fetchImpl });
    reportClientFailure('stream', 'http_error', 502, '87c2b902-a93f-46e3-a066-af38f8360d83');
    await vi.waitFor(() => expect(fetchImpl).toHaveBeenCalledOnce());
    const [url, init] = fetchImpl.mock.calls[0];
    expect(url).toBe('https://api.example/v1/client-diagnostics');
    expect(JSON.parse(init.body)).toEqual({ source: 'stream', failure: 'http_error', status: 502,
      request_id: '87c2b902-a93f-46e3-a066-af38f8360d83' });
    expect(init.body).not.toContain('private-token');
  });

  it('omits invalid identifiers and does not send without a token', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    configureClientDiagnostics({ baseUrl: '/api', getToken: async () => 'token', fetchImpl });
    reportClientFailure('agent', 'invalid_response', 200, 'private user text');
    await vi.waitFor(() => expect(fetchImpl).toHaveBeenCalledOnce());
    expect(JSON.parse(fetchImpl.mock.calls[0][1].body)).toEqual({ source: 'agent', failure: 'invalid_response' });
    configureClientDiagnostics({ baseUrl: '/api', getToken: async () => null, fetchImpl });
    reportClientFailure('agent', 'network');
    await Promise.resolve();
    expect(fetchImpl).toHaveBeenCalledOnce();
  });

  it('reports global errors without their contents and removes listeners', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    configureClientDiagnostics({ baseUrl: '/api', getToken: async () => 'token', fetchImpl });
    const remove = installGlobalDiagnostics();
    window.dispatchEvent(new ErrorEvent('error', { message: 'private detail' }));
    await vi.waitFor(() => expect(fetchImpl).toHaveBeenCalledOnce());
    expect(fetchImpl.mock.calls[0][1].body).not.toContain('private detail');
    remove();
    window.dispatchEvent(new Event('error'));
    await Promise.resolve();
    expect(fetchImpl).toHaveBeenCalledOnce();
  });

  it('retries bounded diagnostic codes when the browser comes back online', async () => {
    const fetchImpl = vi.fn().mockRejectedValueOnce(new TypeError('offline')).mockResolvedValue(new Response(null, { status: 204 }));
    configureClientDiagnostics({ baseUrl: '/api', getToken: async () => 'token', fetchImpl });
    const remove = installGlobalDiagnostics();
    reportClientFailure('sessions', 'network');
    await vi.waitFor(() => expect(fetchImpl).toHaveBeenCalledTimes(1));
    await new Promise((resolve) => setTimeout(resolve, 0));
    window.dispatchEvent(new Event('online'));
    await vi.waitFor(() => expect(fetchImpl).toHaveBeenCalledTimes(2));
    expect(JSON.parse(fetchImpl.mock.calls[1][1].body)).toEqual({ source: 'sessions', failure: 'network' });
    remove();
  });

  it('retries when online arrives while an offline request is still settling', async () => {
    let rejectFirst: (error: Error) => void = () => undefined;
    const fetchImpl = vi.fn().mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectFirst = reject; }))
      .mockResolvedValue(new Response(null, { status: 204 }));
    configureClientDiagnostics({ baseUrl: '/api', getToken: async () => 'token', fetchImpl });
    const remove = installGlobalDiagnostics();
    reportClientFailure('agent', 'network');
    await vi.waitFor(() => expect(fetchImpl).toHaveBeenCalledTimes(1));
    window.dispatchEvent(new Event('online'));
    rejectFirst(new Error('offline'));
    await vi.waitFor(() => expect(fetchImpl).toHaveBeenCalledTimes(2));
    remove();
  });

  it('caps reports per authenticated page session', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    configureClientDiagnostics({ baseUrl: '/api', getToken: async () => 'token', fetchImpl });
    for (let i = 0; i < 15; i += 1) reportClientFailure('agent', 'network');
    await vi.waitFor(() => expect(fetchImpl).toHaveBeenCalledTimes(10));
    await Promise.resolve();
    expect(fetchImpl).toHaveBeenCalledTimes(10);
  });
});
