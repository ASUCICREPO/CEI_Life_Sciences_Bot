import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import type { Page, SessionSummary, SessionsClient } from '../lib/sessionsClient';
import { SessionsClientError } from '../lib/sessionsClient';
import { HistoryPage } from './HistoryPage';

// useSessions always builds a context client (rules of hooks); the injected fake client is what is used.
vi.mock('../lib/auth/AuthProvider', () => ({
  useAuth: () => ({ getAccessToken: async () => 'tok' }),
}));

afterEach(cleanup);

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
        <Route path="/history" element={<HistoryPage client={client} />} />
        <Route path="/tasks" element={<TasksProbe />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe('HistoryPage (real Session API data)', () => {
  it('shows a loading state, then the sessions returned by the API', async () => {
    const listSessions = vi.fn(async () => page([session('a'), session('b', { title: '', message_count: 1 })]));
    renderPage(fakeClient(listSessions));
    expect(screen.getByRole('status').textContent).toMatch(/loading your chats/i);
    expect(await screen.findByText('Chat a')).toBeTruthy();
    expect(screen.getByText('Untitled chat')).toBeTruthy();
    expect(screen.getByText(/1 message ·/)).toBeTruthy();
    expect(listSessions).toHaveBeenCalledWith({ sort: 'updated', limit: 20 });
  });

  it('shows the empty state when the user has no chats', async () => {
    renderPage(fakeClient(vi.fn(async () => page([]))));
    expect(await screen.findByText('No chats yet')).toBeTruthy();
  });

  it('re-queries with the created sort when the user changes the sort', async () => {
    const listSessions = vi.fn(async () => page([session('a')]));
    renderPage(fakeClient(listSessions));
    await screen.findByText('Chat a');
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'created' } });
    await waitFor(() => expect(listSessions).toHaveBeenLastCalledWith({ sort: 'created', limit: 20 }));
  });

  it('loads older chats with the cursor and appends them', async () => {
    const listSessions = vi.fn(async ({ cursor }: { cursor?: string | null } = {}) =>
      cursor === 'c1' ? page([session('old')]) : page([session('new')], 'c1'),
    );
    renderPage(fakeClient(listSessions));
    fireEvent.click(await screen.findByRole('button', { name: /load older chats/i }));
    expect(await screen.findByText('Chat old')).toBeTruthy();
    expect(screen.getByText('Chat new')).toBeTruthy();
    expect(listSessions).toHaveBeenLastCalledWith({ sort: 'updated', limit: 20, cursor: 'c1' });
    expect(screen.queryByRole('button', { name: /load older chats/i })).toBeNull();
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
