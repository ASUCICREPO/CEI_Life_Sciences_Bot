import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { OnboardingPage } from './OnboardingPage';
import { ProfileClientError } from '../lib/profileClient';

const mocks = vi.hoisted(() => ({
  profileClient: { submitProfile: vi.fn() },
  auth: {
    userManager: { signinSilent: vi.fn(async () => {}), removeUser: vi.fn(async () => {}) },
    signIn: vi.fn(async () => {}),
    signOut: vi.fn(async () => {}),
  },
}));

vi.mock('../lib/useProfile', () => ({ useProfileClient: () => mocks.profileClient }));
vi.mock('../lib/auth/AuthProvider', () => ({ useAuth: () => mocks.auth }));

beforeEach(() => {
  mocks.profileClient.submitProfile.mockReset();
  mocks.auth.userManager.signinSilent.mockReset().mockResolvedValue(undefined);
  mocks.auth.userManager.removeUser.mockReset().mockResolvedValue(undefined);
  mocks.auth.signIn.mockReset().mockResolvedValue(undefined);
  mocks.auth.signOut.mockReset().mockResolvedValue(undefined);
});
afterEach(cleanup);

function renderOnboarding() {
  return render(
    <MemoryRouter initialEntries={['/onboarding']}>
      <Routes>
        <Route path="/onboarding" element={<OnboardingPage />} />
        <Route path="/" element={<div>Home page</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

function fillValidForm() {
  fireEvent.change(screen.getByLabelText('First name'), { target: { value: 'Jane' } });
  fireEvent.change(screen.getByLabelText('Last name'), { target: { value: 'Doe' } });
  fireEvent.change(screen.getByLabelText('Date of birth'), { target: { value: '1990-01-01' } });
}

describe('OnboardingPage validation', () => {
  it('shows errors for empty required fields and does not call the API', () => {
    renderOnboarding();
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
    expect(screen.getByText('First name is required.')).toBeTruthy();
    expect(screen.getByText('Last name is required.')).toBeTruthy();
    expect(screen.getByText('Date of birth is required.')).toBeTruthy();
    expect(mocks.profileClient.submitProfile).not.toHaveBeenCalled();
  });

  it('rejects names with disallowed characters', () => {
    renderOnboarding();
    fireEvent.change(screen.getByLabelText('First name'), { target: { value: 'Jane3' } });
    fireEvent.change(screen.getByLabelText('Last name'), { target: { value: 'Doe' } });
    fireEvent.change(screen.getByLabelText('Date of birth'), { target: { value: '1990-01-01' } });
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
    expect(screen.getByText(/First name may only contain/)).toBeTruthy();
    expect(mocks.profileClient.submitProfile).not.toHaveBeenCalled();
  });

  it('rejects a future date of birth', () => {
    renderOnboarding();
    fireEvent.change(screen.getByLabelText('First name'), { target: { value: 'Jane' } });
    fireEvent.change(screen.getByLabelText('Last name'), { target: { value: 'Doe' } });
    fireEvent.change(screen.getByLabelText('Date of birth'), { target: { value: '2999-01-01' } });
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
    expect(screen.getByText('Date of birth cannot be in the future.')).toBeTruthy();
    expect(mocks.profileClient.submitProfile).not.toHaveBeenCalled();
  });

  it('rejects a year before 1900', () => {
    renderOnboarding();
    fireEvent.change(screen.getByLabelText('First name'), { target: { value: 'Jane' } });
    fireEvent.change(screen.getByLabelText('Last name'), { target: { value: 'Doe' } });
    fireEvent.change(screen.getByLabelText('Date of birth'), { target: { value: '1899-01-01' } });
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
    expect(screen.getByText('Date of birth must be in 1900 or later.')).toBeTruthy();
    expect(mocks.profileClient.submitProfile).not.toHaveBeenCalled();
  });

  it('shows a non-blocking note for a date of birth under 18, but still allows submission', async () => {
    mocks.profileClient.submitProfile.mockResolvedValue({ status: 'ok', profile: {} });
    renderOnboarding();
    fireEvent.change(screen.getByLabelText('First name'), { target: { value: 'Jane' } });
    fireEvent.change(screen.getByLabelText('Last name'), { target: { value: 'Doe' } });
    fireEvent.change(screen.getByLabelText('Date of birth'), { target: { value: '2020-01-01' } });
    expect(screen.getByText(/Founder AI requires founders to be 18 or over/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
    await waitFor(() => expect(mocks.profileClient.submitProfile).toHaveBeenCalled());
  });
});

describe('OnboardingPage submission', () => {
  it('on success, refreshes tokens with signinSilent and navigates to /', async () => {
    mocks.profileClient.submitProfile.mockResolvedValue({
      status: 'ok',
      profile: { given_name: 'Jane', family_name: 'Doe', birthdate: '1990-01-01' },
    });
    renderOnboarding();
    fillValidForm();
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
    await waitFor(() => expect(mocks.profileClient.submitProfile).toHaveBeenCalledWith({
      given_name: 'Jane', family_name: 'Doe', birthdate: '1990-01-01',
    }));
    await waitFor(() => expect(mocks.auth.userManager.signinSilent).toHaveBeenCalled());
    expect(await screen.findByText('Home page')).toBeTruthy();
  });

  it('falls back to signIn("/") when the silent refresh fails', async () => {
    mocks.profileClient.submitProfile.mockResolvedValue({ status: 'ok', profile: {} });
    mocks.auth.userManager.signinSilent.mockRejectedValue(new Error('no refresh token'));
    renderOnboarding();
    fillValidForm();
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
    await waitFor(() => expect(mocks.auth.signIn).toHaveBeenCalledWith('/'));
  });

  it('treats 409 profile_exists as success: refreshes tokens and continues to /', async () => {
    mocks.profileClient.submitProfile.mockRejectedValue(
      new ProfileClientError('Profile already set.', { status: 409, code: 'profile_exists' }),
    );
    renderOnboarding();
    fillValidForm();
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
    await waitFor(() => expect(mocks.auth.userManager.signinSilent).toHaveBeenCalled());
    expect(await screen.findByText('Home page')).toBeTruthy();
  });

  it('on 403 age_restricted, shows the closure message and signs out on confirmation', async () => {
    mocks.profileClient.submitProfile.mockRejectedValue(
      new ProfileClientError('You must be 18 or over.', { status: 403, code: 'age_restricted' }),
    );
    renderOnboarding();
    fillValidForm();
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
    expect(await screen.findByText(/Founder AI is only available to people aged 18 or over/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Sign out' }));
    await waitFor(() => expect(mocks.auth.userManager.removeUser).toHaveBeenCalled());
    expect(mocks.auth.signOut).toHaveBeenCalled();
  });

  it('shows a generic error message on an unrelated failure', async () => {
    mocks.profileClient.submitProfile.mockRejectedValue(new ProfileClientError('Could not reach the profile service.'));
    renderOnboarding();
    fillValidForm();
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
    expect(await screen.findByText('Could not reach the profile service.')).toBeTruthy();
  });
});
