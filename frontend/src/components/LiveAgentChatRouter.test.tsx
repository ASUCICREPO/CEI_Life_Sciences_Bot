import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { LiveAgentChat } from './LiveAgentChat';

// F9: free-text router mode (no `agent` prop) always goes through /chat/stream without `task_type`;
// the Session Router classifies it. These tests cover the parts LiveAgentChat.test.tsx does not:
// the free-text transport, the router ChoicePicker pick, and the auto-sent Home prompt.
const mocks = vi.hoisted(() => ({
  streaming: true,
  createSession: vi.fn(),
  streamProps: vi.fn<(props: unknown) => void>(),
  streamViewProps: vi.fn<(props: unknown) => void>(),
  pickEnvelope: {} as Record<string, unknown>,
  modelsClient: { listModels: vi.fn() },
}));

vi.mock('../lib/auth/AuthProvider', () => ({ useAuth: () => ({ config: { researchStreamingEnabled: mocks.streaming } }) }));
vi.mock('../lib/useSessions', () => ({ useSessionsClient: () => ({ createSession: mocks.createSession }) }));
vi.mock('../lib/useModels', () => ({ useModelsClient: () => mocks.modelsClient }));
vi.mock('@tanstack/react-virtual', () => ({
  useVirtualizer: (options: { count: number }) => ({
    getTotalSize: () => options.count * 10,
    getVirtualItems: () => Array.from({ length: options.count }, (_, index) => ({ key: index, index, start: index * 10 })),
    measureElement: () => {},
    scrollToIndex: () => {},
  }),
}));
// StreamView's mock renders a button so a test can drive the router ChoicePicker's
// onInteractionAction without the real reducer/CEIUISurface stack.
vi.mock('./streaming/StreamingSurface', () => ({
  StreamingSurface: (props: unknown) => { mocks.streamProps(props); return <div>Streaming</div>; },
  StreamView: (props: { onInteractionAction?: (seq: number, envelope: unknown) => void }) => {
    mocks.streamViewProps(props);
    return props.onInteractionAction
      ? <button type="button" onClick={() => props.onInteractionAction!(1, mocks.pickEnvelope)}>Pick</button>
      : null;
  },
}));

const routeChoiceAnswer = (value: string, status: 'completed' | 'rejected' | 'stale', message: string | null = null) => ({
  text: status === 'completed' ? 'Starting that now.' : '',
  status,
  action_response: {
    request_id: 'r1', session_id: 's1', surface_id: 'router-choice-run', revision: 1,
    interaction_id: 'router-choice', component_id: 'router-choice', action_id: 'route-choice',
    value, status, message,
  },
});

const chat = (extra: Partial<Parameters<typeof LiveAgentChat>[0]> = {}) => (
  <MemoryRouter>
    <LiveAgentChat mode="science" threadKey="ask" title="Ask" description="Ask" examples={[]} placeholder="Ask" {...extra} />
  </MemoryRouter>
);

beforeEach(() => {
  vi.stubGlobal('crypto', { randomUUID: () => '3b1f6c1e-1d2a-4c5b-9e8f-0123456789ab' });
  mocks.streaming = true;
  mocks.createSession.mockReset();
  mocks.streamProps.mockReset();
  mocks.streamViewProps.mockReset();
  mocks.pickEnvelope = routeChoiceAnswer('drafting', 'completed');
  mocks.modelsClient.listModels.mockReset().mockResolvedValue([]);
});
afterEach(cleanup);

describe('free-text router transport', () => {
  it('starts a stream without task_type when no agent is known yet', async () => {
    mocks.createSession.mockResolvedValue({ session_id: 'server-session' });
    render(chat());
    fireEvent.change(screen.getByLabelText('Ask'), { target: { value: 'What is EGFR?' } });
    fireEvent.submit(screen.getByLabelText('Ask').closest('form')!);
    await waitFor(() => expect(mocks.streamProps).toHaveBeenCalled());
    const props = mocks.streamProps.mock.calls.at(-1)![0] as { body: Record<string, unknown> };
    expect(props.body).toMatchObject({ session_id: 'server-session', message: 'What is EGFR?', mode: 'science' });
    expect(props.body).not.toHaveProperty('task_type');
  });

  it('disables the form when this environment has no streaming yet', () => {
    mocks.streaming = false;
    render(chat());
    expect((screen.getByLabelText('Ask') as HTMLTextAreaElement).disabled).toBe(true);
    expect(screen.getByText(/not available in this environment/i)).toBeTruthy();
    expect(mocks.streamProps).not.toHaveBeenCalled();
  });

  it('sends a prompt carried from Home automatically on mount', async () => {
    mocks.createSession.mockResolvedValue({ session_id: 'server-session' });
    render(chat({ autoSendPrompt: 'Draft a grant outline' }));
    await waitFor(() => expect(mocks.streamProps).toHaveBeenCalled());
    const props = mocks.streamProps.mock.calls.at(-1)![0] as { body: Record<string, unknown> };
    expect(props.body.message).toBe('Draft a grant outline');
    expect(screen.getByText('Draft a grant outline')).toBeTruthy();
  });
});

describe('model picker in free-text mode', () => {
  it('stays unlocked for a new chat, then locks once the chat has a turn (F8 regression)', async () => {
    mocks.modelsClient.listModels.mockResolvedValue([{ model_id: 'sonnet', label: 'Claude Sonnet' }]);
    mocks.createSession.mockResolvedValue({ session_id: 'server-session' });
    render(chat());
    expect(((await screen.findByLabelText('Chat model')) as HTMLSelectElement).disabled).toBe(false);
    fireEvent.change(screen.getByLabelText('Ask'), { target: { value: 'What is EGFR?' } });
    fireEvent.submit(screen.getByLabelText('Ask').closest('form')!);
    await waitFor(() => expect(mocks.streamProps).toHaveBeenCalled());
    expect((screen.getByLabelText('Chat model') as HTMLSelectElement).disabled).toBe(true);
  });
});

describe('router ChoicePicker pick', () => {
  async function startAndPick() {
    mocks.createSession.mockResolvedValue({ session_id: 'server-session' });
    render(chat());
    fireEvent.change(screen.getByLabelText('Ask'), { target: { value: 'Help me plan something' } });
    fireEvent.submit(screen.getByLabelText('Ask').closest('form')!);
    await waitFor(() => expect(mocks.streamProps).toHaveBeenCalled());
    const { onFinished } = mocks.streamProps.mock.calls.at(-1)![0] as {
      onFinished: (state: unknown, connectionError: string | null) => void;
    };
    onFinished({ status: 'awaiting_input', interactions: [{ kind: 'hitl', seq: 1, surface: {} }] }, null);
    fireEvent.click(await screen.findByText('Pick'));
  }

  it('appends the resumed envelope, marks the card answered, and names the chosen agent', async () => {
    await startAndPick();
    expect(await screen.findByText('Starting that now.')).toBeTruthy();
    expect(screen.getByText('Routed to Drafting')).toBeTruthy();
    await waitFor(() => expect(mocks.streamViewProps.mock.calls.at(-1)![0]).toMatchObject({
      answeredInteractions: new Set([1]),
    }));
  });

  it('shows an expired pick like any other failed action, without naming an agent', async () => {
    mocks.pickEnvelope = routeChoiceAnswer('drafting', 'rejected', 'This request has expired. Start again.');
    await startAndPick();
    expect(await screen.findByText('This request has expired. Start again.')).toBeTruthy();
    expect(screen.queryByText(/Routed to/)).toBeNull();
    await waitFor(() => expect(mocks.streamViewProps.mock.calls.at(-1)![0]).toMatchObject({
      answeredInteractions: new Set([1]),
    }));
  });

  it('shows a stale pick like any other failed action', async () => {
    mocks.pickEnvelope = routeChoiceAnswer('analysis', 'stale', 'This request is no longer waiting for that input.');
    await startAndPick();
    expect(await screen.findByText('This request is no longer waiting for that input.')).toBeTruthy();
    expect(screen.queryByText(/Routed to/)).toBeNull();
  });
});

describe('explicit task pages are unaffected', () => {
  it('still sends task_type for a known agent', async () => {
    mocks.createSession.mockResolvedValue({ session_id: 'server-session' });
    render(chat({ agent: 'research', threadKey: 'live-research' }));
    fireEvent.change(screen.getByLabelText('Ask'), { target: { value: 'What is EGFR?' } });
    fireEvent.submit(screen.getByLabelText('Ask').closest('form')!);
    await waitFor(() => expect(mocks.streamProps).toHaveBeenCalled());
    const props = mocks.streamProps.mock.calls.at(-1)![0] as { body: Record<string, unknown> };
    expect(props.body).toMatchObject({ task_type: 'live-research' });
  });
});
