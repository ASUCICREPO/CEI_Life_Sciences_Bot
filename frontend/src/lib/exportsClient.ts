import type { TokenProvider, Page } from './sessionsClient';

export interface ReportExport {
  export_id: string;
  session_id: string;
  turn_id: string;
  filename: string;
  format: string;
  size: number;
  created_at: string;
  status: string;
}

export interface ExportsClient {
  list(sessionId: string, cursor?: string | null): Promise<Page<ReportExport>>;
  download(exportId: string): Promise<string>;
}

export function createExportsClient(getToken: TokenProvider, baseUrl = '/api', fetchImpl: typeof fetch = fetch): ExportsClient {
  const base = baseUrl.replace(/\/+$/, '');
  async function request(path: string): Promise<unknown> {
    const token = await getToken();
    if (!token) throw new Error('Sign in again to access your reports.');
    const response = await fetchImpl(`${base}${path}`, {
      headers: { accept: 'application/json', authorization: `Bearer ${token}` },
    });
    if (!response.ok) throw new Error('Could not access your reports. Please try again.');
    return response.json();
  }
  return {
    async list(sessionId, cursor) {
      const query = cursor ? `?cursor=${encodeURIComponent(cursor)}` : '';
      const result = await request(`/sessions/${encodeURIComponent(sessionId)}/exports${query}`);
      if (!result || typeof result !== 'object' || !Array.isArray((result as Page<ReportExport>).items)) {
        throw new Error('The report list could not be read.');
      }
      return result as Page<ReportExport>;
    },
    async download(exportId) {
      const result = await request(`/exports/${encodeURIComponent(exportId)}/download`);
      const url = result && typeof result === 'object' ? (result as { download_url?: unknown }).download_url : undefined;
      if (typeof url !== 'string') throw new Error('The download link could not be read.');
      let parsed: URL;
      try { parsed = new URL(url); } catch { throw new Error('The download link could not be read.'); }
      if (parsed.protocol !== 'https:') throw new Error('The download link could not be read.');
      return parsed.href;
    },
  };
}
