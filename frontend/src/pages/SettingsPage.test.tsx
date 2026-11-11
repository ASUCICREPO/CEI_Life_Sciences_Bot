import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { SettingsPage } from './SettingsPage';
import { ModelsClientError } from '../lib/modelsClient';
import { AppProvider } from '../context/AppContext';
import { ProductTourProvider } from '../components/tour/ProductTour';

// ADR: one stable client object | a fresh object per render would change useModelsClient's identity
// on every setState and re-trigger ModelSettingsSection's load effect (its dependency array).
const mocks = vi.hoisted(() => ({
  client: { listModels: vi.fn(), getSettings: vi.fn(), updateSettings: vi.fn() },
  auth: { config: null as { accountDeletionEnabled?: boolean } | null, getAccessToken: vi.fn(async () => 'token'), signOut: vi.fn(async () => {}) },
}));

vi.mock('../lib/useModels', () => ({ useModelsClient: () => mocks.client }));
// ADR: mock AuthProvider here too | DeleteAccountSection (added below) reads the flag from useAuth().config,
// same mocking pattern as ReportExports.test.tsx.
vi.mock('../lib/auth/AuthProvider', () => ({ useAuth: () => mocks.auth }));

beforeEach(() => {
  mocks.client.listModels.mockReset();
  mocks.client.getSettings.mockReset();
  mocks.client.updateSettings.mockReset();
  mocks.auth.config = null;
});
afterEach(cleanup);

describe('SettingsPage model and mode defaults', () => {
  it('loads the model list and current settings, then shows them selected', async () => {
    mocks.client.listModels.mockResolvedValue([
      { model_id: 'us.anthropic.claude-sonnet-4-5', label: 'Claude Sonnet' },
      { model_id: 'us.anthropic.claude-haiku-4-5', label: 'Claude Haiku' },
    ]);
    mocks.client.getSettings.mockResolvedValue({ default_model_id: 'us.anthropic.claude-haiku-4-5', default_mode: 'business', configured_sources: [] });
    render(<MemoryRouter><AppProvider><ProductTourProvider><SettingsPage /></ProductTourProvider></AppProvider></MemoryRouter>);
    await waitFor(() => expect((screen.getByLabelText('Default model') as HTMLSelectElement).value).toBe('us.anthropic.claude-haiku-4-5'));
    expect((screen.getByLabelText('Default chat mode') as HTMLSelectElement).value).toBe('business');
    expect(screen.getByRole('option', { name: 'Claude Sonnet' })).toBeTruthy();
  });

  it('saves a changed default model and mode', async () => {
    mocks.client.listModels.mockResolvedValue([{ model_id: 'us.anthropic.claude-sonnet-4-5', label: 'Claude Sonnet' }]);
    mocks.client.getSettings.mockResolvedValue({ default_model_id: null, default_mode: null, configured_sources: [] });
    mocks.client.updateSettings.mockResolvedValue({ default_model_id: 'us.anthropic.claude-sonnet-4-5', default_mode: 'science', configured_sources: [] });
    render(<MemoryRouter><AppProvider><ProductTourProvider><SettingsPage /></ProductTourProvider></AppProvider></MemoryRouter>);
    await waitFor(() => expect(screen.getByLabelText('Default model')).toBeTruthy());
    fireEvent.change(screen.getByLabelText('Default model'), { target: { value: 'us.anthropic.claude-sonnet-4-5' } });
    fireEvent.change(screen.getByLabelText('Default chat mode'), { target: { value: 'science' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(mocks.client.updateSettings).toHaveBeenCalledWith({ default_model_id: 'us.anthropic.claude-sonnet-4-5', default_mode: 'science' }));
    expect(await screen.findByText('Saved.')).toBeTruthy();
  });

  it('shows the 400 invalid_request message clearly when the save is rejected', async () => {
    mocks.client.listModels.mockResolvedValue([{ model_id: 'blocked-model', label: 'Blocked' }]);
    mocks.client.getSettings.mockResolvedValue({ default_model_id: null, default_mode: null, configured_sources: [] });
    mocks.client.updateSettings.mockRejectedValue(new ModelsClientError('The default_model_id field is not an available model.', { status: 400, code: 'invalid_request' }));
    render(<MemoryRouter><AppProvider><ProductTourProvider><SettingsPage /></ProductTourProvider></AppProvider></MemoryRouter>);
    await waitFor(() => expect(screen.getByLabelText('Default model')).toBeTruthy());
    fireEvent.change(screen.getByLabelText('Default model'), { target: { value: 'blocked-model' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'The default_model_id field is not an available model.');
  });

  it('shows a load error instead of the form when the settings service fails', async () => {
    mocks.client.listModels.mockResolvedValue([]);
    mocks.client.getSettings.mockRejectedValue(new ModelsClientError('Could not reach the settings service.'));
    render(<MemoryRouter><AppProvider><ProductTourProvider><SettingsPage /></ProductTourProvider></AppProvider></MemoryRouter>);
    expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'Could not reach the settings service.');
    expect(screen.queryByLabelText('Default model')).toBeNull();
  });
});

describe('SettingsPage delete account section', () => {
  beforeEach(() => {
    mocks.client.listModels.mockResolvedValue([]);
    mocks.client.getSettings.mockResolvedValue({ default_model_id: null, default_mode: null, configured_sources: [] });
  });

  it('is not rendered when accountDeletionEnabled is off', async () => {
    mocks.auth.config = null;
    render(<MemoryRouter><AppProvider><ProductTourProvider><SettingsPage /></ProductTourProvider></AppProvider></MemoryRouter>);
    await waitFor(() => expect(screen.queryByLabelText('Default model')).toBeTruthy());
    expect(screen.queryByRole('heading', { name: 'Delete account' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Delete account' })).toBeNull();
  });

  it('is rendered when accountDeletionEnabled is on', async () => {
    mocks.auth.config = { accountDeletionEnabled: true };
    render(<MemoryRouter><AppProvider><ProductTourProvider><SettingsPage /></ProductTourProvider></AppProvider></MemoryRouter>);
    await waitFor(() => expect(screen.queryByLabelText('Default model')).toBeTruthy());
    expect(screen.getByRole('heading', { name: 'Delete account' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Delete account' })).toBeTruthy();
  });
});
