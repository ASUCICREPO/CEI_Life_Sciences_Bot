import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { CEIUISurface } from './cei-ui-surface';
import type { CEIUISurface as CEIUISurfaceType } from '../../lib/cei-ui/schema';

const mocks = vi.hoisted(() => ({ token: vi.fn(async () => 'token'), fetch: vi.fn() }));
vi.mock('../../lib/auth/AuthProvider', () => ({ useAuth: () => ({ getAccessToken: mocks.token, config: { apiBaseUrl: '/api' } }) }));

beforeEach(() => {
  mocks.fetch.mockReset();
  vi.stubGlobal('fetch', mocks.fetch);
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

const EXPORT_SURFACE: CEIUISurfaceType = {
  content_type: 'application/vnd.cei.ui+json',
  protocol: 'cei-ui',
  version: '1.1',
  surface_id: 'drafting-complete',
  revision: 1,
  root_id: 'root',
  components: [
    { id: 'root', type: 'Column', children: ['summary', 'file'] },
    { id: 'summary', type: 'Text', variant: 'body', content: 'Your grant draft is ready.' },
    {
      id: 'file',
      type: 'ExportFile',
      export_id: '12345678-1234-1234-1234-123456789abc_0123456789abcdef0123456789abcdef',
      filename: 'TP53-grant.pdf',
      format: 'pdf',
      byte_size: 204_800,
    },
  ],
};

describe('ExportFile card', () => {
  it('renders the format, filename, and human size', () => {
    render(<CEIUISurface surface={EXPORT_SURFACE} agent="drafting" sessionId="s-1" />);
    expect(screen.getByText('TP53-grant.pdf')).toBeTruthy();
    expect(screen.getByText(/PDF/)).toBeTruthy();
    expect(screen.getByText(/200(\.0)? KB/)).toBeTruthy();
  });

  it('downloads via exportsClient and opens the returned link', async () => {
    mocks.fetch.mockResolvedValueOnce({ ok: true, json: async () => ({ download_url: 'https://example.com/file.pdf' }) });
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    render(<CEIUISurface surface={EXPORT_SURFACE} agent="drafting" sessionId="s-1" />);
    fireEvent.click(screen.getByRole('button', { name: 'Download TP53-grant.pdf' }));
    expect(screen.getByText('Preparing…')).toBeTruthy();
    await waitFor(() => expect(click).toHaveBeenCalledOnce());
    expect(mocks.fetch.mock.calls[0][0]).toBe('/api/exports/12345678-1234-1234-1234-123456789abc_0123456789abcdef0123456789abcdef/download');
  });

  it('shows an error if the download link cannot be fetched', async () => {
    mocks.fetch.mockRejectedValueOnce(new Error('offline'));
    render(<CEIUISurface surface={EXPORT_SURFACE} agent="drafting" sessionId="s-1" />);
    fireEvent.click(screen.getByRole('button', { name: 'Download TP53-grant.pdf' }));
    expect(await screen.findByRole('alert')).toBeTruthy();
  });
});
