import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { AppProvider } from '../context/AppContext';
import { AskPage } from './AskPage';

const mocks = vi.hoisted(() => ({ liveAgentChatProps: vi.fn<(props: unknown) => void>() }));

// AskPage is a thin wrapper: it is enough to check it reaches LiveAgentChat with no `agent`
// (free-text routing, F9) and forwards Home's typed prompt; LiveAgentChat has its own tests.
vi.mock('../components/LiveAgentChat', () => ({
  LiveAgentChat: (props: unknown) => { mocks.liveAgentChatProps(props); return <div>Chat</div>; },
}));

afterEach(cleanup);

function renderAsk(initialPrompt?: string) {
  return render(
    <MemoryRouter initialEntries={[{ pathname: '/ask', state: initialPrompt ? { prompt: initialPrompt } : undefined }]}>
      <AppProvider>
        <Routes>
          <Route path="/ask" element={<AskPage />} />
        </Routes>
      </AppProvider>
    </MemoryRouter>,
  );
}

describe('AskPage', () => {
  it('renders LiveAgentChat without an agent so the Router classifies the turn', () => {
    renderAsk();
    expect(screen.getByText('Chat')).toBeTruthy();
    const props = mocks.liveAgentChatProps.mock.calls.at(-1)![0] as Record<string, unknown>;
    expect(props.agent).toBeUndefined();
    expect(props.threadKey).toBe('ask');
    expect(props.autoSendPrompt).toBeUndefined();
  });

  it('forwards a prompt carried in route state as autoSendPrompt', () => {
    renderAsk('What is EGFR?');
    const props = mocks.liveAgentChatProps.mock.calls.at(-1)![0] as Record<string, unknown>;
    expect(props.autoSendPrompt).toBe('What is EGFR?');
  });
});
