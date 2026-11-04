import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { getLoadedRuntimeConfig, loadRuntimeConfig, resetRuntimeConfigForTests } from './config';

const BASE = {
  region: 'us-east-1',
  userPoolId: 'us-east-1_abc',
  userPoolClientId: 'client123',
  cognitoDomain: 'https://cei-ls-1.auth.us-east-1.amazoncognito.com',
};

function serve(body: unknown, status = 200) {
  const fetchMock = vi.fn(async () => new Response(JSON.stringify(body), { status }));
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

describe('loadRuntimeConfig', () => {
  beforeEach(() => resetRuntimeConfigForTests());
  afterEach(() => resetRuntimeConfigForTests());

  it('keeps the Cognito fields and exposes the config synchronously once loaded', async () => {
    serve(BASE);
    expect(getLoadedRuntimeConfig()).toBeNull();
    const config = await loadRuntimeConfig();
    expect(config).toEqual(BASE);
    expect(getLoadedRuntimeConfig()).toEqual(BASE);
  });

  it('keeps apiBaseUrl and strips a trailing slash', async () => {
    serve({ ...BASE, apiBaseUrl: 'https://abc.execute-api.us-east-1.amazonaws.com/prod/' });
    expect((await loadRuntimeConfig()).apiBaseUrl).toBe('https://abc.execute-api.us-east-1.amazonaws.com/prod');
  });

  it('accepts a same-origin path apiBaseUrl', async () => {
    serve({ ...BASE, apiBaseUrl: '/api' });
    expect((await loadRuntimeConfig()).apiBaseUrl).toBe('/api');
  });

  it('leaves apiBaseUrl undefined when absent', async () => {
    serve(BASE);
    expect((await loadRuntimeConfig()).apiBaseUrl).toBeUndefined();
  });

  it('rejects a non-https absolute apiBaseUrl', async () => {
    serve({ ...BASE, apiBaseUrl: 'http://abc.execute-api.us-east-1.amazonaws.com/prod' });
    await expect(loadRuntimeConfig()).rejects.toThrow(/apiBaseUrl/);
  });

  it('keeps https agentUrls and drops non-https or unknown entries', async () => {
    serve({
      ...BASE,
      agentUrls: {
        research: 'https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/r/invocations?qualifier=DEFAULT',
        drafting: 'http://insecure.example.com/invocations',
        router: 'https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/x/invocations',
      },
    });
    expect((await loadRuntimeConfig()).agentUrls).toEqual({
      research: 'https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/r/invocations?qualifier=DEFAULT',
    });
  });

  it('still rejects an invalid cognitoDomain', async () => {
    serve({ ...BASE, cognitoDomain: 'http://not-https.example.com' });
    await expect(loadRuntimeConfig()).rejects.toThrow(/cognitoDomain/);
  });

  it('resets the cache after a failed fetch so a retry refetches', async () => {
    serve({}, 500);
    await expect(loadRuntimeConfig()).rejects.toThrow(/HTTP 500/);
    const retry = serve(BASE);
    await expect(loadRuntimeConfig()).resolves.toEqual(BASE);
    expect(retry).toHaveBeenCalledTimes(1);
  });
});
