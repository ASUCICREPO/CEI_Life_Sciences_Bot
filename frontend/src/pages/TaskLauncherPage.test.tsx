import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { AppProvider } from '../context/AppContext';
import { ProductTourProvider } from '../components/tour/ProductTour';
import { TaskLauncherPage } from './TaskLauncherPage';

// Isolates this page from the real live-chat stack (auth, sessions, virtualizer, streaming); the live-agent
// wiring itself (agent routing, threadKey, streaming transport) is covered by LiveAgentChat's own tests.
const mocks = vi.hoisted(() => ({ liveAgentChat: vi.fn() }));
vi.mock('../components/LiveAgentChat', () => ({
  LiveAgentChat: (props: Record<string, unknown>) => {
    mocks.liveAgentChat(props);
    return <div data-testid="live-agent-chat">{String(props.threadKey)}</div>;
  },
}));

function AskProbe() {
  const location = useLocation();
  return <pre data-testid="ask-route-state">{JSON.stringify(location.state)}</pre>;
}

function renderPage() {
  // The tour's own welcome overlay is unrelated to this page's behavior; skip it so it never covers content.
  localStorage.setItem('founder-ai-product-tour-v3', 'skipped');
  return render(
    <MemoryRouter initialEntries={['/tasks']}>
      <AppProvider>
        <ProductTourProvider>
          <Routes>
            <Route path="/tasks" element={<TaskLauncherPage />} />
            <Route path="/ask" element={<AskProbe />} />
          </Routes>
        </ProductTourProvider>
      </AppProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  mocks.liveAgentChat.mockReset();
});
afterEach(() => {
  cleanup();
  localStorage.clear();
});

describe('TaskLauncherPage task picker', () => {
  it('lists only the groups whose tasks open a live agent', () => {
    renderPage();
    expect(screen.getByText('Landscape Analysis')).toBeTruthy();
    expect(screen.getByText('Research and Drafting')).toBeTruthy();
    expect(screen.getByText('Run a Pipeline')).toBeTruthy();
    // Groups that only ever opened a mock chat are gone entirely.
    expect(screen.queryByText('Understand the Science')).toBeNull();
    expect(screen.queryByText('Validate a Target')).toBeNull();
    expect(screen.queryByText('Look at Molecules')).toBeNull();
    expect(screen.queryByText('Map the Path to Market')).toBeNull();
    expect(screen.queryByText('Watch the Landscape')).toBeNull();
    expect(screen.queryByText('Produce a Deliverable')).toBeNull();
    expect(screen.queryByText('Answer a Big Question')).toBeNull();
  });

  it('has no mock "Start from your area" verticals or routing hint', () => {
    renderPage();
    expect(screen.queryByText('Start from your area')).toBeNull();
    expect(screen.queryByText(/I think this is a/)).toBeNull();
  });

  it('collapses the five old drafting tasks into one "Draft a document" task', () => {
    renderPage();
    fireEvent.click(screen.getByText('Research and Drafting'));
    expect(screen.getByText('Draft a document')).toBeTruthy();
    expect(screen.queryByText('Draft a grant')).toBeNull();
    expect(screen.queryByText('Draft a VC pitch')).toBeNull();
    expect(screen.queryByText('Draft a journal article')).toBeNull();
    expect(screen.queryByText('Draft a whitepaper')).toBeNull();
    expect(screen.queryByText('Draft a LaTeX document')).toBeNull();
  });
});

describe('New chat and Just ask', () => {
  it('"New chat" opens the real Ask chat with no initial prompt', () => {
    renderPage();
    fireEvent.click(screen.getByRole('button', { name: 'New chat' }));
    expect(screen.getByTestId('ask-route-state').textContent).toBe('null');
  });

  it('"Just ask" opens the real Ask chat and carries the typed text as the first prompt', () => {
    renderPage();
    fireEvent.change(screen.getByLabelText('Search tasks or describe your need'), { target: { value: 'What is EGFR?' } });
    fireEvent.click(screen.getByRole('button', { name: 'Just ask' }));
    expect(JSON.parse(screen.getByTestId('ask-route-state').textContent ?? 'null')).toEqual({ prompt: 'What is EGFR?' });
  });
});

describe('selecting a live task', () => {
  it('opens the one drafting task without a document_type prop', () => {
    renderPage();
    fireEvent.click(screen.getByText('Research and Drafting'));
    fireEvent.click(screen.getByText('Draft a document'));
    expect(screen.getByTestId('live-agent-chat').textContent).toBe('live-drafting');
    const props = mocks.liveAgentChat.mock.calls.at(-1)![0] as Record<string, unknown>;
    expect(props.agent).toBe('drafting');
    expect(props).not.toHaveProperty('documentType');
  });

  it('opens the research chat task', () => {
    renderPage();
    fireEvent.click(screen.getByText('Research and Drafting'));
    fireEvent.click(screen.getByText('Research chat'));
    const props = mocks.liveAgentChat.mock.calls.at(-1)![0] as Record<string, unknown>;
    expect(props.agent).toBe('research');
    expect(props.threadKey).toBe('live-research');
  });
});
