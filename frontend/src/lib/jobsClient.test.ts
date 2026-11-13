import { describe, expect, it, vi } from 'vitest';
import { createJobsClient } from './jobsClient';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

const JOB = {
  job_id: 'job_0000000001000_00000000000000ab', session_id: 's-1', agent_type: 'pipeline_manager',
  status: 'running', progress_pct: 50, error: null, cancel_requested: false,
  created_at: '2026-11-10T00:00:00.000000Z', updated_at: '2026-11-10T00:01:00.000000Z',
};

describe('createJobsClient', () => {
  it('never calls the API without an access token', async () => {
    const fetchImpl = vi.fn();
    const client = createJobsClient(async () => null, '/api', fetchImpl);
    await expect(client.list()).rejects.toThrow(/Sign in/);
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it('lists jobs with the bearer token and an encoded cursor, never a user id', async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({ items: [JOB], next_cursor: 'n', has_more: true }));
    const client = createJobsClient(async () => 'tok', 'https://api.example/v1/', fetchImpl);
    const page = await client.list('a/b');
    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('https://api.example/v1/jobs?cursor=a%2Fb');
    expect((init.headers as Record<string, string>).authorization).toBe('Bearer tok');
    expect(url).not.toContain('user_id');
    expect(page).toEqual({ items: [JOB], next_cursor: 'n', has_more: true });
  });

  it('rejects a malformed job list', async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({ items: [{ job_id: 1 }] }));
    await expect(createJobsClient(async () => 'tok', '/api', fetchImpl).list()).rejects.toThrow(/could not be read/);
  });

  it('cancels with POST to the encoded job path', async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({ job_id: JOB.job_id, status: 'cancelling' }, 202));
    const status = await createJobsClient(async () => 'tok', '/api', fetchImpl).cancel('job/../x');
    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('/api/jobs/job%2F..%2Fx/cancel');
    expect(init.method).toBe('POST');
    expect(status).toBe('cancelling');
  });

  it('surfaces a safe message on an error response', async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({ error: { code: 'not_found', message: 'secret detail' } }, 404));
    await expect(createJobsClient(async () => 'tok', '/api', fetchImpl).cancel('j')).rejects.toThrow('Could not reach the job service. Please try again.');
  });
});
