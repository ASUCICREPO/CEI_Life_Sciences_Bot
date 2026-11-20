import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { RequireOnboarding, RequireIncompleteProfile } from './RequireOnboarding';

const mocks = vi.hoisted(() => ({ auth: { user: null as { profile: Record<string, unknown> } | null } }));

vi.mock('./AuthProvider', () => ({ useAuth: () => mocks.auth }));

afterEach(cleanup);

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/onboarding" element={<RequireIncompleteProfile><div>Onboarding form</div></RequireIncompleteProfile>} />
        <Route element={<RequireOnboarding><div>App shell</div></RequireOnboarding>}>
          <Route path="/" element={<div>Home</div>} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

describe('RequireOnboarding', () => {
  it('sends an authenticated user with an incomplete profile to /onboarding', () => {
    mocks.auth.user = { profile: { given_name: 'Jane' } };
    renderAt('/');
    expect(screen.getByText('Onboarding form')).toBeTruthy();
    expect(screen.queryByText('App shell')).toBeNull();
  });

  it('renders the protected route once the profile is complete', () => {
    mocks.auth.user = { profile: { given_name: 'Jane', family_name: 'Doe', birthdate: '1990-01-01' } };
    renderAt('/');
    expect(screen.getByText('App shell')).toBeTruthy();
  });
});

describe('RequireIncompleteProfile', () => {
  it('sends a completed profile away from /onboarding to /', () => {
    mocks.auth.user = { profile: { given_name: 'Jane', family_name: 'Doe', birthdate: '1990-01-01' } };
    renderAt('/onboarding');
    expect(screen.getByText('App shell')).toBeTruthy();
    expect(screen.queryByText('Onboarding form')).toBeNull();
  });

  it('lets an incomplete profile stay on /onboarding', () => {
    mocks.auth.user = { profile: {} };
    renderAt('/onboarding');
    expect(screen.getByText('Onboarding form')).toBeTruthy();
  });
});
