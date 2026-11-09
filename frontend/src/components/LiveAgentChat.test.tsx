import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { LiveAgentChat } from './LiveAgentChat';

const mocks = vi.hoisted(() => ({
  streaming: true,
  createSession: vi.fn(),
  invokeAgent: vi.fn(),
  streamProps: vi.fn<(props: unknown) => void>(),
}));

vi.mock('../lib/auth/AuthProvider', () => ({ useAuth: () => ({ config: { researchStreamingEnabled: mocks.streaming } }) }));
vi.mock('../lib/useSessions', () => ({ useSessionsClient: () => ({ createSession: mocks.createSession }) }));
vi.mock('../lib/agentClient', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../lib/agentClient')>()), invokeAgent: mocks.invokeAgent,
}));
vi.mock('@tanstack/react-virtual', () => ({
  useVirtualizer: () => ({ getTotalSize: () => 0, getVirtualItems: () => [], measureElement: () => {}, scrollToIndex: () => {} }),
}));
vi.mock('./streaming/StreamingSurface', () => ({
  StreamingSurface: (props: unknown) => { mocks.streamProps(props); return <div>Streaming</div>; },
  StreamView: () => null,
}));

beforeEach(() => {
  vi.stubGlobal('crypto', { randomUUID: () => '3b1f6c1e-1d2a-4c5b-9e8f-0123456789ab' });
  mocks.streaming = true;
  mocks.createSession.mockReset();
  mocks.invokeAgent.mockReset();
  mocks.streamProps.mockReset();
});
afterEach(cleanup);

describe('Research chat transport', () => {
  it('creates a server session and sends a stable run identifier to the streaming component', async () => {
    mocks.createSession.mockResolvedValue({ session_id: 'server-session' });
    render(<MemoryRouter><LiveAgentChat agent="research" mode="science" threadKey="live-research"
      title="Research" description="Research" examples={[]} placeholder="Ask" /></MemoryRouter>);
    fireEvent.change(screen.getByLabelText('Ask'), { target: { value: '  What is EGFR?  ' } });
    fireEvent.submit(screen.getByLabelText('Ask').closest('form')!);
    await waitFor(() => expect(mocks.streamProps).toHaveBeenCalled());
    expect(mocks.createSession).toHaveBeenCalledWith({ title: 'What is EGFR?', mode: 'science' });
    const props = mocks.streamProps.mock.calls.at(-1)![0] as { sessionId: string; body: Record<string, unknown> };
    expect(props.sessionId).toBe('server-session');
    expect(props.body).toMatchObject({ session_id: 'server-session', message: 'What is EGFR?', mode: 'science', task_type: 'live-research' });
    expect(props.body.run_id).toMatch(/^[0-9a-f]{8}-[0-9a-f-]{27}$/i);
    expect(mocks.invokeAgent).not.toHaveBeenCalled();
  });

  it('keeps the existing agent invocation when streaming is disabled', async () => {
    mocks.streaming = false;
    mocks.invokeAgent.mockResolvedValue({ text: 'Result' });
    render(<MemoryRouter><LiveAgentChat agent="research" mode="science" threadKey="live-research"
      title="Research" description="Research" examples={[]} placeholder="Ask" /></MemoryRouter>);
    fireEvent.change(screen.getByLabelText('Ask'), { target: { value: 'question' } });
    fireEvent.submit(screen.getByLabelText('Ask').closest('form')!);
    await waitFor(() => expect(mocks.invokeAgent).toHaveBeenCalledTimes(1));
    expect(mocks.createSession).not.toHaveBeenCalled();
    expect(mocks.streamProps).not.toHaveBeenCalled();
  });
});
