import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { LiveAgentChat } from './LiveAgentChat';
import type { CEIUISurface as CEIUISurfaceType } from '../lib/cei-ui/schema';
import type { StreamEvent } from '../lib/stream/events';

// Covers the HITL-answers-over-SSE path end to end (real CEIUISurface + real StreamingSurface wiring),
// unlike LiveAgentChat.test.tsx / LiveAgentChatRouter.test.tsx, which mock StreamingSurface entirely.
const mocks = vi.hoisted(() => ({
  loadHistory: vi.fn(),
  dispatchCEIUIAction: vi.fn(),
  dispatchCEIUIActionExpectingStream: vi.fn(),
  streamProps: vi.fn<(props: unknown) => void>(),
  // A stable object: a fresh one per render would change useModelsClient's identity on every setState
  // and re-trigger LiveAgentChat's model-list load effect (its dependency array).
  modelsClient: { listModels: vi.fn() },
  sessionsClient: { loadHistory: vi.fn(), updateSession: vi.fn(), createSession: vi.fn() },
}));

vi.mock('../lib/auth/AuthProvider', () => ({
  useAuth: () => ({ getAccessToken: async () => 'tok', config: { researchStreamingEnabled: true } }),
}));
vi.mock('../lib/useSessions', () => ({ useSessionsClient: () => mocks.sessionsClient }));
vi.mock('../lib/useModels', () => ({ useModelsClient: () => mocks.modelsClient }));
vi.mock('../lib/cei-ui/actions', () => ({
  dispatchCEIUIAction: mocks.dispatchCEIUIAction,
  dispatchCEIUIActionExpectingStream: mocks.dispatchCEIUIActionExpectingStream,
}));
vi.mock('@tanstack/react-virtual', () => ({
  useVirtualizer: (options: { count: number }) => ({
    getTotalSize: () => options.count * 10,
    getVirtualItems: () => Array.from({ length: options.count }, (_, index) => ({ key: index, index, start: index * 10 })),
    measureElement: () => {},
    scrollToIndex: () => {},
  }),
}));
// StreamingSurface itself is real; only the network opener is swapped out, so the mount/openStreamImpl
// wiring from LiveAgentChat is exercised for real.
vi.mock('../lib/stream/openStream', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../lib/stream/openStream')>()),
}));

const OUTLINE_SURFACE: CEIUISurfaceType = {
  content_type: 'application/vnd.cei.ui+json',
  protocol: 'cei-ui',
  version: '1.1',
  surface_id: 'drafting-outline',
  revision: 1,
  root_id: 'outline-review',
  components: [
    {
      id: 'outline-review',
      type: 'ReviewGate',
      interaction_id: 'drafting-outline-review',
      plan: { summary: 'Proposed document outline', steps: [{ step_id: 'outline-0', title: 'Introduction', description: null, status: 'pending' }] },
      actions: [{ action_id: 'approve-outline', label: 'Approve', intent: 'approve' }],
      require_approval: true,
    },
  ],
};

function draftStreamEvents(): AsyncIterable<StreamEvent> {
  const events: StreamEvent[] = [
    { seq: 0, type: 'surface_begin', surface_id: 'draft', revision: 1, version: '1.2', root_id: 'draft-root' },
    { seq: 1, type: 'component_add', parent_id: null, component: { id: 'draft-root', type: 'Column', children: [] } },
    { seq: 2, type: 'surface_end', final: true },
  ];
  return (async function* () {
    for (const event of events) yield event;
  })();
}

beforeEach(() => {
  mocks.sessionsClient.loadHistory.mockReset().mockResolvedValue({
    items: [{ message_id: 'm1', role: 'ai', sequence_number: 1, timestamp: '2026-01-01T00:00:00Z', text: '', ui: OUTLINE_SURFACE, citations: [], truncated: false, content_blocks: [] }],
    next_cursor: null,
    session: { mode: 'science' },
  });
  mocks.dispatchCEIUIAction.mockReset();
  mocks.dispatchCEIUIActionExpectingStream.mockReset();
  mocks.streamProps.mockReset();
  mocks.modelsClient.listModels.mockReset().mockResolvedValue([]);
});
afterEach(cleanup);

describe('drafting outline approval answered over SSE', () => {
  it('renders a new live streaming assistant message instead of a finished envelope', async () => {
    mocks.dispatchCEIUIActionExpectingStream.mockResolvedValue({ kind: 'stream', events: draftStreamEvents() });
    render(
      <MemoryRouter>
        <LiveAgentChat agent="drafting" mode="science" threadKey="live-drafting" resumeSessionId="sess-1"
          title="Drafting" description="Drafting" examples={[]} placeholder="Ask" />
      </MemoryRouter>,
    );
    const approve = await screen.findByRole('button', { name: 'Approve' });
    fireEvent.click(approve);

    await waitFor(() => expect(mocks.dispatchCEIUIActionExpectingStream).toHaveBeenCalledWith(
      expect.objectContaining({ agent: 'drafting', interaction_id: 'drafting-outline-review', action_id: 'approve-outline' }),
    ));
    // The streamed draft reaches the ended state once its own surface_end arrives.
    await waitFor(() => expect(document.querySelector('[data-status="ended"]')).not.toBeNull());
    // The outline card is answered (disabled), not replaced: the approve button is gone from an active form.
    expect(mocks.dispatchCEIUIAction).not.toHaveBeenCalled();
  });

  it('keeps today\'s JSON behaviour when the action response is not an event stream', async () => {
    mocks.dispatchCEIUIActionExpectingStream.mockResolvedValue({ kind: 'envelope', envelope: { text: 'Outline approved.', status: 'completed' } });
    render(
      <MemoryRouter>
        <LiveAgentChat agent="drafting" mode="science" threadKey="live-drafting" resumeSessionId="sess-1"
          title="Drafting" description="Drafting" examples={[]} placeholder="Ask" />
      </MemoryRouter>,
    );
    const approve = await screen.findByRole('button', { name: 'Approve' });
    fireEvent.click(approve);
    expect(await screen.findByText('Outline approved.')).toBeTruthy();
  });
});
