import type { Page, TokenProvider } from './sessionsClient';

/** One row of GET /jobs (api.md Jobs, B98); the server returns only the caller's jobs. */
export interface JobSummary {
  job_id: string;
  session_id: string;
  agent_type: string;
  status: string;
  progress_pct: number;
  error: string | null;
  cancel_requested: boolean;
  created_at: string;
  updated_at: string;
}

export interface JobsClient {
  list(cursor?: string | null): Promise<Page<JobSummary>>;
  cancel(jobId: string): Promise<string>;
}

export const TERMINAL_JOB_STATUSES = new Set(['completed', 'partial', 'failed', 'cancelled']);

function isJob(value: unknown): value is JobSummary {
  if (!value || typeof value !== 'object') return false;
  const job = value as Record<string, unknown>;
  return typeof job.job_id === 'string' && typeof job.session_id === 'string' && typeof job.status === 'string';
}

export function createJobsClient(getToken: TokenProvider, baseUrl = '/api', fetchImpl: typeof fetch = fetch): JobsClient {
  const base = baseUrl.replace(/\/+$/, '');
  async function request(path: string, method: 'GET' | 'POST' = 'GET'): Promise<unknown> {
    const token = await getToken();
    if (!token) throw new Error('Sign in again to see your background jobs.');
    const response = await fetchImpl(`${base}${path}`, {
      method,
      headers: { accept: 'application/json', authorization: `Bearer ${token}` },
    });
    if (!response.ok) throw new Error('Could not reach the job service. Please try again.');
    return response.json();
  }
  return {
    async list(cursor) {
      const query = cursor ? `?cursor=${encodeURIComponent(cursor)}` : '';
      const result = await request(`/jobs${query}`);
      const page = result as Page<unknown> | null;
      if (!page || typeof page !== 'object' || !Array.isArray(page.items) || !page.items.every(isJob)) {
        throw new Error('The job list could not be read.');
      }
      return { items: page.items as JobSummary[], next_cursor: page.next_cursor ?? null, has_more: Boolean(page.has_more) };
    },
    async cancel(jobId) {
      const result = await request(`/jobs/${encodeURIComponent(jobId)}/cancel`, 'POST');
      const status = result && typeof result === 'object' ? (result as { status?: unknown }).status : undefined;
      if (typeof status !== 'string') throw new Error('The job service returned an unexpected response.');
      return status;
    },
  };
}
