import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { DeleteAccountDialog } from './DeleteAccountDialog';
import { AccountClientError } from '../lib/accountClient';

const mocks = vi.hoisted(() => ({
  navigate: vi.fn(),
  accountClient: { deleteAccount: vi.fn() },
  auth: { signOut: vi.fn(async () => {}) },
}));

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom');
  return { ...actual, useNavigate: () => mocks.navigate };
});
vi.mock('../lib/useAccount', () => ({ useAccountClient: () => mocks.accountClient }));
vi.mock('../lib/auth/AuthProvider', () => ({ useAuth: () => mocks.auth }));

function renderDialog(onClose = vi.fn()) {
  render(<MemoryRouter><DeleteAccountDialog open onClose={onClose} /></MemoryRouter>);
  return onClose;
}

beforeEach(() => {
  mocks.navigate.mockReset();
  mocks.accountClient.deleteAccount.mockReset();
  mocks.auth.signOut.mockReset();
  mocks.auth.signOut.mockResolvedValue(undefined);
  sessionStorage.setItem('cei-session:probe', 'x');
  localStorage.setItem('cei-tour-status', 'x');
});
afterEach(() => { cleanup(); sessionStorage.clear(); localStorage.clear(); });

describe('DeleteAccountDialog', () => {
  it('keeps the confirm button disabled until DELETE is typed exactly', () => {
    renderDialog();
    const confirmButton = screen.getByRole('button', { name: 'Delete my account' }) as HTMLButtonElement;
    expect(confirmButton.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText('Type DELETE to confirm'), { target: { value: 'delete' } });
    expect(confirmButton.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText('Type DELETE to confirm'), { target: { value: 'DELETE' } });
    expect(confirmButton.disabled).toBe(false);
  });

  it('on 202: calls the API once, clears local caches, signs out, and navigates to sign-in', async () => {
    mocks.accountClient.deleteAccount.mockResolvedValue({ status: 'deleting' });
    renderDialog();
    fireEvent.change(screen.getByLabelText('Type DELETE to confirm'), { target: { value: 'DELETE' } });
    fireEvent.click(screen.getByRole('button', { name: 'Delete my account' }));
    await waitFor(() => expect(mocks.auth.signOut).toHaveBeenCalledTimes(1));
    expect(mocks.accountClient.deleteAccount).toHaveBeenCalledTimes(1);
    expect(sessionStorage.getItem('cei-session:probe')).toBeNull();
    expect(localStorage.getItem('cei-tour-status')).toBeNull();
    expect(mocks.navigate).toHaveBeenCalledWith('/', { replace: true });
  });

  it('on error: shows the message and does not sign the user out', async () => {
    mocks.accountClient.deleteAccount.mockRejectedValue(
      new AccountClientError('Could not reach the account service.', { retriable: true }),
    );
    renderDialog();
    fireEvent.change(screen.getByLabelText('Type DELETE to confirm'), { target: { value: 'DELETE' } });
    fireEvent.click(screen.getByRole('button', { name: 'Delete my account' }));
    expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'Could not reach the account service.');
    expect(mocks.auth.signOut).not.toHaveBeenCalled();
    expect(mocks.navigate).not.toHaveBeenCalled();
    expect(sessionStorage.getItem('cei-session:probe')).toBe('x');
  });
});
