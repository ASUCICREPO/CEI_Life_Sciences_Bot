import { describe, expect, it, vi } from 'vitest';
import { createProfileClient, ProfileClientError } from './profileClient';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

describe('createProfileClient', () => {
  it('never calls the API without an access token', async () => {
    const fetchImpl = vi.fn();
    const client = createProfileClient(async () => null, { fetchImpl });
    await expect(
      client.submitProfile({ given_name: 'Jane', family_name: 'Doe', birthdate: '1990-01-01' }),
    ).rejects.toMatchObject({ status: 401, code: 'unauthorized' });
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it('POSTs the profile with the bearer token and returns it on 200', async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse({ status: 'ok', profile: { given_name: 'Jane', family_name: 'Doe', birthdate: '1990-01-01' } }),
    );
    const client = createProfileClient(async () => 'tok-123', { fetchImpl, baseUrl: 'https://api.example/v1/' });
    const result = await client.submitProfile({ given_name: 'Jane', family_name: 'Doe', birthdate: '1990-01-01' });
    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('https://api.example/v1/profile');
    expect(init.method).toBe('POST');
    expect((init.headers as Record<string, string>).authorization).toBe('Bearer tok-123');
    expect(JSON.parse(init.body as string)).toEqual({ given_name: 'Jane', family_name: 'Doe', birthdate: '1990-01-01' });
    expect(result).toEqual({ status: 'ok', profile: { given_name: 'Jane', family_name: 'Doe', birthdate: '1990-01-01' } });
  });

  it('maps the 409 profile_exists envelope to ProfileClientError', async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse({ error: { code: 'profile_exists', message: 'Profile already set.', retriable: false } }, 409),
    );
    const client = createProfileClient(async () => 'tok', { fetchImpl });
    const error = await client.submitProfile({ given_name: 'Jane', family_name: 'Doe', birthdate: '1990-01-01' }).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ProfileClientError);
    expect(error).toMatchObject({ status: 409, code: 'profile_exists', message: 'Profile already set.' });
  });

  it('maps the 403 age_restricted envelope to ProfileClientError', async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse({ error: { code: 'age_restricted', message: 'You must be 18 or over.', retriable: false } }, 403),
    );
    const client = createProfileClient(async () => 'tok', { fetchImpl });
    const error = await client.submitProfile({ given_name: 'Jane', family_name: 'Doe', birthdate: '2020-01-01' }).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ProfileClientError);
    expect(error).toMatchObject({ status: 403, code: 'age_restricted' });
  });

  it('maps the 400 invalid_request envelope to ProfileClientError', async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse({ error: { code: 'invalid_request', message: 'The birthdate field is invalid.', retriable: false } }, 400),
    );
    const client = createProfileClient(async () => 'tok', { fetchImpl });
    const error = await client.submitProfile({ given_name: 'Jane', family_name: 'Doe', birthdate: 'nope' }).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ProfileClientError);
    expect(error).toMatchObject({ status: 400, code: 'invalid_request', message: 'The birthdate field is invalid.' });
  });

  it('rejects a response without a valid profile shape', async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({ status: 'ok' }));
    const client = createProfileClient(async () => 'tok', { fetchImpl });
    await expect(
      client.submitProfile({ given_name: 'Jane', family_name: 'Doe', birthdate: '1990-01-01' }),
    ).rejects.toBeInstanceOf(ProfileClientError);
  });
});
