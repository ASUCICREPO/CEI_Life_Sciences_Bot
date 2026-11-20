import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { AppProvider } from '../context/AppContext';
import { HomePage } from './HomePage';

vi.mock('../lib/useSessions', () => ({
  useSessions: () => ({ sessions: [], isLoading: false, error: null, hasMore: false, loadMore: () => {}, reload: () => {} }),
  resumeState: (session: unknown) => session,
}));

vi.mock('../lib/auth/AuthProvider', () => ({
  useAuth: () => ({ user: { profile: { email: 'jane.doe@example.com' } } }),
}));

afterEach(cleanup);

function AskProbe() {
  const location = useLocation();
  return <pre data-testid="route-state">{JSON.stringify(location.state)}</pre>;
}

function renderHome() {
  return render(
    <MemoryRouter initialEntries={['/']}>
      <AppProvider>
        <Routes>
          <Route path="/" element={<HomePage />} />
          <Route path="/ask" element={<AskProbe />} />
        </Routes>
      </AppProvider>
    </MemoryRouter>,
  );
}

describe('HomePage greeting', () => {
  it('greets the signed-in user by name, not a placeholder', () => {
    renderHome();
    const heading = screen.getByRole('heading', { level: 1 });
    expect(heading.textContent).toMatch(/^Good (morning|afternoon|evening), Jane$/);
  });
});

describe('HomePage free-text entry', () => {
  it('navigates to /ask with the typed prompt in route state', async () => {
    renderHome();
    fireEvent.change(screen.getByLabelText('Ask Founder AI'), { target: { value: '  What is EGFR?  ' } });
    fireEvent.submit(screen.getByLabelText('Ask Founder AI').closest('form')!);
    const state = JSON.parse((await screen.findByTestId('route-state')).textContent ?? 'null');
    expect(state).toEqual({ prompt: 'What is EGFR?' });
  });

  it('does not navigate on an empty prompt', () => {
    renderHome();
    fireEvent.submit(screen.getByLabelText('Ask Founder AI').closest('form')!);
    expect(screen.queryByTestId('route-state')).toBeNull();
  });
});
