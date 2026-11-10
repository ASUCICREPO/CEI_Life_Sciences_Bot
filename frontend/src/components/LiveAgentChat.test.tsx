import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { LiveAgentChat } from './LiveAgentChat';

const mocks = vi.hoisted(() => ({
  streaming: true,
  createSession: vi.fn(),
  invokeAgent: vi.fn(),
  streamProps: vi.fn<(props: unknown) => void>(),
  streamViewProps: vi.fn<(props: unknown) => void>(),
  // A stable object: a fresh one per render would change useModelsClient's identity on every
  // setState and re-trigger LiveAgentChat's model-list load effect (its dependency array).
  modelsClient: { listModels: vi.fn() },
}));

vi.mock('../lib/auth/AuthProvider', () => ({ useAuth: () => ({ config: { researchStreamingEnabled: mocks.streaming } }) }));
vi.mock('../lib/useSessions', () => ({ useSessionsClient: () => ({ createSession: mocks.createSession }) }));
vi.mock('../lib/useModels', () => ({ useModelsClient: () => mocks.modelsClient }));
vi.mock('../lib/agentClient', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../lib/agentClient')>()), invokeAgent: mocks.invokeAgent,
}));
// Renders every row (no real windowing) so tests can find message content by text.
vi.mock('@tanstack/react-virtual', () => ({
  useVirtualizer: (options: { count: number }) => ({
    getTotalSize: () => options.count * 10,
    getVirtualItems: () => Array.from({ length: options.count }, (_, index) => ({ key: index, index, start: index * 10 })),
    measureElement: () => {},
    scrollToIndex: () => {},
  }),
}));
// StreamView's mock renders a button so tests can drive a hitl interaction's onInteractionAction
// without the real reducer/CEIUISurface stack (covered separately in StreamingSurface.test.tsx).
vi.mock('./streaming/StreamingSurface', () => ({
  StreamingSurface: (props: unknown) => { mocks.streamProps(props); return <div>Streaming</div>; },
  StreamView: (props: { onInteractionAction?: (seq: number, envelope: unknown) => void }) => {
    mocks.streamViewProps(props);
    return props.onInteractionAction
      ? <button type="button" onClick={() => props.onInteractionAction!(1, { text: 'Resumed', status: 'completed' })}>Answer</button>
      : null;
  },
}));

beforeEach(() => {
  vi.stubGlobal('crypto', { randomUUID: () => '3b1f6c1e-1d2a-4c5b-9e8f-0123456789ab' });
  mocks.streaming = true;
  mocks.createSession.mockReset();
  mocks.invokeAgent.mockReset();
  mocks.streamProps.mockReset();
  mocks.streamViewProps.mockReset();
  mocks.modelsClient.listModels.mockReset().mockResolvedValue([]);
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

describe('model picker', () => {
  it('hides the picker until the model list loads', async () => {
    mocks.modelsClient.listModels.mockResolvedValue([{ model_id: 'us.anthropic.claude-sonnet-4-5', label: 'Claude Sonnet' }]);
    render(<MemoryRouter><LiveAgentChat agent="research" mode="science" threadKey="live-research"
      title="Research" description="Research" examples={[]} placeholder="Ask" /></MemoryRouter>);
    expect(screen.queryByLabelText('Chat model')).toBeNull();
    expect(await screen.findByLabelText('Chat model')).toBeTruthy();
  });

  it('includes the picked model in a new session\'s createSession call', async () => {
    mocks.modelsClient.listModels.mockResolvedValue([{ model_id: 'us.anthropic.claude-sonnet-4-5', label: 'Claude Sonnet' }]);
    mocks.createSession.mockResolvedValue({ session_id: 'server-session' });
    render(<MemoryRouter><LiveAgentChat agent="research" mode="science" threadKey="live-research"
      title="Research" description="Research" examples={[]} placeholder="Ask" /></MemoryRouter>);
    fireEvent.change(await screen.findByLabelText('Chat model'), { target: { value: 'us.anthropic.claude-sonnet-4-5' } });
    fireEvent.change(screen.getByLabelText('Ask'), { target: { value: 'question' } });
    fireEvent.submit(screen.getByLabelText('Ask').closest('form')!);
    await waitFor(() => expect(mocks.createSession).toHaveBeenCalledWith({ title: 'question', mode: 'science', model_id: 'us.anthropic.claude-sonnet-4-5' }));
  });
});

describe('stream hitl answer', () => {
  it('answers a terminal stream hitl card and appends the resumed turn', async () => {
    mocks.createSession.mockResolvedValue({ session_id: 'server-session' });
    render(<MemoryRouter><LiveAgentChat agent="research" mode="science" threadKey="live-research"
      title="Research" description="Research" examples={[]} placeholder="Ask" /></MemoryRouter>);
    fireEvent.change(screen.getByLabelText('Ask'), { target: { value: 'question' } });
    fireEvent.submit(screen.getByLabelText('Ask').closest('form')!);
    await waitFor(() => expect(mocks.streamProps).toHaveBeenCalled());

    // Simulate the stream reaching a terminal hitl_request: StreamingSurface hands the state to onFinished.
    const { onFinished } = mocks.streamProps.mock.calls.at(-1)![0] as {
      onFinished: (state: unknown, connectionError: string | null) => void;
    };
    onFinished({ status: 'awaiting_input', interactions: [{ kind: 'hitl', seq: 1, surface: {} }] }, null);

    const answer = await screen.findByText('Answer');
    fireEvent.click(answer);
    expect(await screen.findByText('Resumed')).toBeTruthy();
    await waitFor(() => expect(mocks.streamViewProps.mock.calls.at(-1)![0]).toMatchObject({
      answeredInteractions: new Set([1]),
    }));
  });
});
