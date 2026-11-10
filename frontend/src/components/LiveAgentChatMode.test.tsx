import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { LiveAgentChat } from './LiveAgentChat';

const mocks = vi.hoisted(() => ({
  client: { loadHistory: vi.fn(), updateSession: vi.fn(), createSession: vi.fn() },
  stream: vi.fn<(props: unknown) => void>(),
  // A stable object: see LiveAgentChat.test.tsx for why a fresh one per render would loop the load effect.
  modelsClient: { listModels: vi.fn() },
}));
vi.mock('../lib/auth/AuthProvider', () => ({ useAuth: () => ({ config: { researchStreamingEnabled: true } }) }));
vi.mock('../lib/useSessions', () => ({ useSessionsClient: () => mocks.client }));
vi.mock('../lib/useModels', () => ({ useModelsClient: () => mocks.modelsClient }));
vi.mock('@tanstack/react-virtual', () => ({
  useVirtualizer: () => ({ getTotalSize: () => 0, getVirtualItems: () => [], measureElement: () => {}, scrollToIndex: () => {} }),
}));
vi.mock('./streaming/StreamingSurface', () => ({
  StreamingSurface: (props: unknown) => { mocks.stream(props); return <div>Streaming</div>; },
  StreamView: () => null,
}));

const chat = (resumeSessionId?: string) => <MemoryRouter><LiveAgentChat agent="research" mode="science"
  threadKey="live-research" title="Research" description="Research" examples={[]} placeholder="Ask"
  resumeSessionId={resumeSessionId} /></MemoryRouter>;

beforeEach(() => {
  vi.stubGlobal('crypto', { randomUUID: () => '3b1f6c1e-1d2a-4c5b-9e8f-0123456789ab' });
  Object.values(mocks.client).forEach((mock) => mock.mockReset());
  mocks.stream.mockReset();
  mocks.modelsClient.listModels.mockReset().mockResolvedValue([]);
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('per-chat mode', () => {
  it('uses the selected mode when a new streaming chat is created and invoked', async () => {
    mocks.client.createSession.mockResolvedValue({ session_id: 'new-session' });
    render(chat());
    fireEvent.change(screen.getByLabelText('Chat mode'), { target: { value: 'business' } });
    fireEvent.change(screen.getByLabelText('Ask'), { target: { value: 'Question' } });
    fireEvent.submit(screen.getByLabelText('Ask').closest('form')!);
    await waitFor(() => expect(mocks.stream).toHaveBeenCalled());
    expect(mocks.client.createSession).toHaveBeenCalledWith({ title: 'Question', mode: 'business' });
    expect(mocks.stream.mock.calls.at(-1)![0]).toMatchObject({ body: { mode: 'business' } });
  });

  it('restores stored mode and persists a change before showing it', async () => {
    mocks.client.loadHistory.mockResolvedValue({ items: [], next_cursor: null, session: { mode: 'business' } });
    mocks.client.updateSession.mockResolvedValue({});
    render(chat('existing-session'));
    await waitFor(() => expect((screen.getByLabelText('Chat mode') as HTMLSelectElement).value).toBe('business'));
    fireEvent.change(screen.getByLabelText('Chat mode'), { target: { value: 'science' } });
    await waitFor(() => expect(mocks.client.updateSession).toHaveBeenCalledWith('existing-session', { mode: 'science' }));
    await waitFor(() => expect((screen.getByLabelText('Chat mode') as HTMLSelectElement).value).toBe('science'));
  });

  it('keeps the saved mode when the update fails', async () => {
    mocks.client.loadHistory.mockResolvedValue({ items: [], next_cursor: null, session: { mode: 'business' } });
    mocks.client.updateSession.mockRejectedValue(new Error('offline'));
    render(chat('existing-session'));
    await waitFor(() => expect((screen.getByLabelText('Chat mode') as HTMLSelectElement).value).toBe('business'));
    fireEvent.change(screen.getByLabelText('Chat mode'), { target: { value: 'science' } });
    expect((await screen.findByRole('alert')).textContent).toContain('Could not update');
    expect((screen.getByLabelText('Chat mode') as HTMLSelectElement).value).toBe('business');
  });
});

describe('per-chat model', () => {
  const models = [{ model_id: 'haiku', label: 'Claude Haiku' }, { model_id: 'sonnet', label: 'Claude Sonnet' }];

  it('restores the session model and persists a change via PATCH', async () => {
    mocks.modelsClient.listModels.mockResolvedValue(models);
    mocks.client.loadHistory.mockResolvedValue({ items: [], next_cursor: null, session: { mode: 'business', model_id: 'haiku' } });
    mocks.client.updateSession.mockResolvedValue({});
    render(chat('existing-session'));
    await waitFor(() => expect((screen.getByLabelText('Chat model') as HTMLSelectElement).value).toBe('haiku'));
    fireEvent.change(screen.getByLabelText('Chat model'), { target: { value: 'sonnet' } });
    await waitFor(() => expect(mocks.client.updateSession).toHaveBeenCalledWith('existing-session', { model_id: 'sonnet' }));
    await waitFor(() => expect((screen.getByLabelText('Chat model') as HTMLSelectElement).value).toBe('sonnet'));
  });

  it('keeps the saved model when the PATCH fails', async () => {
    mocks.modelsClient.listModels.mockResolvedValue(models);
    mocks.client.loadHistory.mockResolvedValue({ items: [], next_cursor: null, session: { mode: 'business', model_id: 'haiku' } });
    mocks.client.updateSession.mockRejectedValue(new Error('offline'));
    render(chat('existing-session'));
    await waitFor(() => expect((screen.getByLabelText('Chat model') as HTMLSelectElement).value).toBe('haiku'));
    fireEvent.change(screen.getByLabelText('Chat model'), { target: { value: 'sonnet' } });
    expect((await screen.findByRole('alert')).textContent).toContain('Could not update');
    expect((screen.getByLabelText('Chat model') as HTMLSelectElement).value).toBe('haiku');
  });
});
