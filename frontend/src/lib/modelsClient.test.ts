import { describe, expect, it, vi } from 'vitest';
import { createModelsClient, ModelsClientError } from './modelsClient';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

describe('createModelsClient', () => {
  it('never calls the API without an access token', async () => {
    const fetchImpl = vi.fn();
    const client = createModelsClient(async () => null, { fetchImpl });
    await expect(client.listModels()).rejects.toMatchObject({ status: 401, code: 'unauthorized' });
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it('lists server-discovered models with the bearer token', async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({ models: [{ model_id: 'us.anthropic.claude-sonnet-4-5', label: 'Claude Sonnet' }] }));
    const client = createModelsClient(async () => 'tok-123', { fetchImpl, baseUrl: 'https://api.example/v1/' });
    const models = await client.listModels();
    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('https://api.example/v1/models');
    expect((init.headers as Record<string, string>).authorization).toBe('Bearer tok-123');
    expect(models).toEqual([{ model_id: 'us.anthropic.claude-sonnet-4-5', label: 'Claude Sonnet' }]);
  });

  it('drops malformed model entries instead of throwing', async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({ models: [{ model_id: 'ok', label: 'Ok' }, { model_id: 'bad' }] }));
    const client = createModelsClient(async () => 'tok', { fetchImpl });
    await expect(client.listModels()).resolves.toEqual([{ model_id: 'ok', label: 'Ok' }]);
  });

  it('reads settings with configured_sources defaulted to an empty list', async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({ default_model_id: null, default_mode: null, configured_sources: [] }));
    const client = createModelsClient(async () => 'tok', { fetchImpl });
    await expect(client.getSettings()).resolves.toEqual({ default_model_id: null, default_mode: null, configured_sources: [] });
  });

  it('PUTs a settings patch and returns the updated settings', async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({ default_model_id: 'haiku', default_mode: 'business', configured_sources: ['pubmed'] }));
    const client = createModelsClient(async () => 'tok', { fetchImpl, baseUrl: 'https://api.example/v1' });
    const result = await client.updateSettings({ default_model_id: 'haiku', default_mode: 'business' });
    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('https://api.example/v1/settings');
    expect(init.method).toBe('PUT');
    expect(JSON.parse(init.body as string)).toEqual({ default_model_id: 'haiku', default_mode: 'business' });
    expect(result).toEqual({ default_model_id: 'haiku', default_mode: 'business', configured_sources: ['pubmed'] });
  });

  it('maps the api.md error envelope to ModelsClientError on an invalid_request', async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse({ error: { code: 'invalid_request', message: 'The default_model_id field is not an available model.', retriable: false, request_id: 'r1' } }, 400),
    );
    const client = createModelsClient(async () => 'tok', { fetchImpl });
    const error = await client.updateSettings({ default_model_id: 'blocked-model' }).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ModelsClientError);
    expect(error).toMatchObject({
      status: 400,
      code: 'invalid_request',
      message: 'The default_model_id field is not an available model.',
      requestId: 'r1',
      retriable: false,
    });
  });

  it('treats 5xx as retriable', async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({}, 503));
    const client = createModelsClient(async () => 'tok', { fetchImpl });
    await expect(client.getSettings()).rejects.toMatchObject({ status: 503, retriable: true });
  });

  it('rejects a models response without a models array', async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({ nope: true }));
    const client = createModelsClient(async () => 'tok', { fetchImpl });
    await expect(client.listModels()).rejects.toBeInstanceOf(ModelsClientError);
  });
});
