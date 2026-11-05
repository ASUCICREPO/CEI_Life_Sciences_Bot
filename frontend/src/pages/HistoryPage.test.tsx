import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import type { Page, SessionSummary, SessionsClient } from '../lib/sessionsClient';
import { SessionsClientError } from '../lib/sessionsClient';
import { HistoryPage } from './HistoryPage';

// useSessionPages always builds a context client (rules of hooks); the injected fake client is what is used.
vi.mock('../lib/auth/AuthProvider', () => ({
  useAuth: () => ({ getAccessToken: async () => 'tok' }),
}));

afterEach(cleanup);

type ListArgs = Parameters<SessionsClient['listSessions']>[0];

function session(id: string, overrides: Partial<SessionSummary> = {}): SessionSummary {
  return {
    session_id: id,
    title: `Chat ${id}`,
    title_source: 'generated',
    status: 'active',
    agent_type: 'research',
    mode: 'science',
    message_count: 2,
    created_at: '2026-01-02T00:00:00Z',
    updated_at: '2026-01-03T00:00:00Z',
    last_activity_at: '2026-01-04T00:00:00Z',
    ...overrides,
  };
}

function page(items: SessionSummary[], next: string | null = null): Page<SessionSummary> {
  return { items, next_cursor: next, has_more: next !== null };
}

function fakeClient(listSessions: SessionsClient['listSessions']): SessionsClient {
  const unused = () => Promise.reject(new Error('not used'));
  return { listSessions, getSession: unused, createSession: unused, updateSession: unused, loadHistory: unused };
}

function TasksProbe() {
  const location = useLocation();
  return <pre data-testid="route-state">{JSON.stringify(location.state)}</pre>;
}

function renderPage(client: SessionsClient) {
  return render(
    <MemoryRouter initialEntries={['/history']}>
      <Routes>
        <Route path="/history" element={<HistoryPage client={client} searchDebounceMs={0} />} />
        <Route path="/tasks" element={<TasksProbe />} />
      </Routes>
    </MemoryRouter>,
  );
}

const firstPage = { sort: 'activity', limit: 25, cursor: null, q: null };

describe('HistoryPage (real Session API data)', () => {
  it('shows a loading state, then page 1 sorted by last activity, 25 per page', async () => {
    const listSessions = vi.fn(async () => page([session('a'), session('b', { title: '', message_count: 1 })]));
    renderPage(fakeClient(listSessions));
    expect(screen.getByRole('status').textContent).toMatch(/loading your chats/i);
    expect(await screen.findByText('Chat a')).toBeTruthy();
    expect(screen.getByText('Untitled chat')).toBeTruthy();
    expect(screen.getByText(/1 message ·/)).toBeTruthy();
    expect(listSessions).toHaveBeenCalledTimes(1);
    expect(listSessions).toHaveBeenCalledWith(firstPage);
    expect((screen.getByRole('combobox') as HTMLSelectElement).value).toBe('activity');
    expect(screen.getAllByText(/^Last used/).length).toBe(2);
  });

  it('shows the empty state when the user has no chats', async () => {
    renderPage(fakeClient(vi.fn(async () => page([]))));
    expect(await screen.findByText('No chats yet')).toBeTruthy();
    expect(screen.queryByRole('navigation', { name: /chat history pages/i })).toBeNull();
  });

  it('re-queries from page 1 with the created sort when the user changes the sort', async () => {
    const listSessions = vi.fn(async () => page([session('a')]));
    renderPage(fakeClient(listSessions));
    await screen.findByText('Chat a');
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'created' } });
    await waitFor(() => expect(listSessions).toHaveBeenLastCalledWith({ ...firstPage, sort: 'created' }));
  });

  it('pages forward with the next cursor and back with the cursor stack', async () => {
    const listSessions = vi.fn(async ({ cursor }: ListArgs = {}) => {
      if (cursor === 'c2') return page([session('p3')]);
      if (cursor === 'c1') return page([session('p2')], 'c2');
      return page([session('p1')], 'c1');
    });
    renderPage(fakeClient(listSessions));
    expect(await screen.findByText('Chat p1')).toBeTruthy();
    expect((screen.getByRole('button', { name: /previous/i }) as HTMLButtonElement).disabled).toBe(true);

    fireEvent.click(screen.getByRole('button', { name: /next/i }));
    expect(await screen.findByText('Chat p2')).toBeTruthy();
    expect(screen.queryByText('Chat p1')).toBeNull(); // pages replace, not append
    expect(screen.getByText('Page 2')).toBeTruthy();
    expect(listSessions).toHaveBeenLastCalledWith({ ...firstPage, cursor: 'c1' });

    fireEvent.click(screen.getByRole('button', { name: /next/i }));
    expect(await screen.findByText('Chat p3')).toBeTruthy();
    expect((screen.getByRole('button', { name: /next/i }) as HTMLButtonElement).disabled).toBe(true);

    fireEvent.click(screen.getByRole('button', { name: /previous/i }));
    expect(await screen.findByText('Chat p2')).toBeTruthy();
    expect(listSessions).toHaveBeenLastCalledWith({ ...firstPage, cursor: 'c1' });

    fireEvent.click(screen.getByRole('button', { name: /previous/i }));
    expect(await screen.findByText('Chat p1')).toBeTruthy();
    expect(listSessions).toHaveBeenLastCalledWith(firstPage);
  });

  it('searches by chat title with a trimmed q and starts again at page 1', async () => {
    const listSessions = vi.fn(async ({ cursor, q }: ListArgs = {}) => {
      if (q) return page([session('hit', { title: 'CRISPR screen' })]);
      return cursor === 'c1' ? page([session('p2')]) : page([session('p1')], 'c1');
    });
    renderPage(fakeClient(listSessions));
    await screen.findByText('Chat p1');
    fireEvent.click(screen.getByRole('button', { name: /next/i }));
    await screen.findByText('Chat p2');

    const search = screen.getByRole('searchbox', { name: /search chats by title/i }) as HTMLInputElement;
    expect(search.maxLength).toBe(100);
    fireEvent.change(search, { target: { value: '  crispr  ' } });
    expect(await screen.findByText('CRISPR screen')).toBeTruthy();
    expect(listSessions).toHaveBeenLastCalledWith({ ...firstPage, q: 'crispr' });
    expect(screen.queryByText('Page 2')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: /clear search/i }));
    expect(await screen.findByText('Chat p1')).toBeTruthy();
    expect(listSessions).toHaveBeenLastCalledWith(firstPage);
  });

  it('shows a no-matches state (not the no-chats state) for an empty search', async () => {
    const listSessions = vi.fn(async ({ q }: ListArgs = {}) => (q ? page([]) : page([session('a')])));
    renderPage(fakeClient(listSessions));
    await screen.findByText('Chat a');
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'nothing' } });
    expect(await screen.findByText(/No chats match/)).toBeTruthy();
    expect(screen.queryByText('No chats yet')).toBeNull();
  });

  it('does not send a whitespace-only search', async () => {
    const listSessions = vi.fn(async () => page([session('a')]));
    renderPage(fakeClient(listSessions));
    await screen.findByText('Chat a');
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: '   ' } });
    await waitFor(() => expect(screen.queryByRole('status')).toBeNull());
    for (const [args] of listSessions.mock.calls as unknown as [ListArgs][]) expect(args?.q).toBeNull();
  });

  it('shows the API error and retries on demand', async () => {
    const listSessions = vi
      .fn<SessionsClient['listSessions']>()
      .mockRejectedValueOnce(new SessionsClientError('You are signed out. Sign in again to see your chats.'))
      .mockResolvedValue(page([session('a')]));
    renderPage(fakeClient(listSessions));
    expect((await screen.findByRole('alert')).textContent).toMatch(/signed out/i);
    fireEvent.click(screen.getByRole('button', { name: /retry/i }));
    expect(await screen.findByText('Chat a')).toBeTruthy();
  });

  it('opens a chat by navigating to /tasks with the session id in route state', async () => {
    renderPage(fakeClient(vi.fn(async () => page([session('abc', { agent_type: 'drafting', document_type: 'grant' })]))));
    fireEvent.click(await screen.findByText('Chat abc'));
    const state = JSON.parse((await screen.findByTestId('route-state')).textContent ?? 'null');
    expect(state).toEqual({ sessionId: 'abc', agentType: 'drafting', analysisType: null, documentType: 'grant' });
  });
});
