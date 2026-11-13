import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { ReportExports } from './ReportExports';

const mocks = vi.hoisted(() => ({ token: vi.fn(async () => 'token'), fetch: vi.fn() }));
vi.mock('../lib/auth/AuthProvider', () => ({ useAuth: () => ({ getAccessToken: mocks.token, config: { apiBaseUrl: '/api' } }) }));

beforeEach(() => {
  mocks.fetch.mockReset();
  vi.stubGlobal('fetch', mocks.fetch);
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe('generated reports', () => {
  it('lists metadata without requesting a link, then requests one on click', async () => {
    mocks.fetch.mockResolvedValueOnce({ ok: true, json: async () => ({
      items: [{ export_id: 'exp-1', filename: 'report.pdf', status: 'ready' }], next_cursor: null,
    }) });
    mocks.fetch.mockResolvedValueOnce({ ok: true, json: async () => ({ download_url: 'https://example.com/report' }) });
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    render(<ReportExports sessionId="session-1" />);
    await screen.findByText('report.pdf');
    expect(mocks.fetch).toHaveBeenCalledTimes(1);
    expect(mocks.fetch.mock.calls[0][0]).toBe('/api/sessions/session-1/exports');
    fireEvent.click(screen.getByRole('button', { name: 'Download report.pdf' }));
    await waitFor(() => expect(click).toHaveBeenCalledOnce());
    expect(mocks.fetch.mock.calls[1][0]).toBe('/api/exports/exp-1/download');
    expect(mocks.fetch.mock.calls[1][1].headers.authorization).toBe('Bearer token');
  });

  it('reports failure and lets the user retry', async () => {
    mocks.fetch.mockRejectedValueOnce(new Error('offline'));
    mocks.fetch.mockResolvedValueOnce({ ok: true, json: async () => ({ items: [], next_cursor: null }) });
    render(<ReportExports sessionId="session-1" />);
    await screen.findByRole('alert');
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('No reports in this chat yet.')).toBeTruthy();
  });
});
