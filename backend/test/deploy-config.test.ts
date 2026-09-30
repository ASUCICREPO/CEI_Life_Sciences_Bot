import {
  parseDeployConfig,
  DeployConfigError,
  DEFAULT_LIMITS,
} from '../lib/config/deploy-config';

function validRaw(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    region: 'us-east-1',
    defaultModelId: 'anthropic.claude-3-5-sonnet-20241022-v2:0',
    routerModelId: 'anthropic.claude-3-haiku-20240307-v1:0',
    blockedModelIds: ['amazon.titan-text-express-v1'],
    allowedEmailDomains: ['example.com'],
    adminEmail: 'admin@example.com',
    ...overrides,
  };
}

describe('parseDeployConfig', () => {
  it('parses a valid config', () => {
    const config = parseDeployConfig(validRaw());
    expect(config.region).toBe('us-east-1');
    expect(config.defaultModelId).toBe('anthropic.claude-3-5-sonnet-20241022-v2:0');
    expect(config.routerModelId).toBe('anthropic.claude-3-haiku-20240307-v1:0');
    expect(config.blockedModelIds).toEqual(['amazon.titan-text-express-v1']);
    expect(config.allowedEmailDomains).toEqual(['example.com']);
    expect(config.adminEmail).toBe('admin@example.com');
  });

  it('applies default limits, webSearchEnabled=false, and allowLocalhostCallback=false when missing', () => {
    const config = parseDeployConfig(validRaw());
    expect(config.limits).toEqual(DEFAULT_LIMITS);
    expect(config.webSearchEnabled).toBe(false);
    expect(config.allowLocalhostCallback).toBe(false);
  });

  it('accepts an explicit allowLocalhostCallback', () => {
    const config = parseDeployConfig(validRaw({ allowLocalhostCallback: true }));
    expect(config.allowLocalhostCallback).toBe(true);
  });

  it('rejects a non-boolean allowLocalhostCallback', () => {
    let error: DeployConfigError | undefined;
    try {
      parseDeployConfig(validRaw({ allowLocalhostCallback: 'true' }));
    } catch (err) {
      error = err as DeployConfigError;
    }
    expect(error).toBeInstanceOf(DeployConfigError);
    expect(error!.errors.some((e) => e.includes('allowLocalhostCallback'))).toBe(true);
  });

  it('applies per-field limit defaults when limits is partially specified', () => {
    const config = parseDeployConfig(validRaw({ limits: { requestsPerSecond: 50 } }));
    expect(config.limits).toEqual({
      requestsPerSecond: 50,
      maxRealtimeRunsPerUser: DEFAULT_LIMITS.maxRealtimeRunsPerUser,
      maxBackgroundRunsPerUser: DEFAULT_LIMITS.maxBackgroundRunsPerUser,
    });
  });

  it('accepts an explicit webSearchEnabled and repository', () => {
    const config = parseDeployConfig(
      validRaw({ webSearchEnabled: true, repository: { url: 'https://github.com/org/repo', branch: 'main' } }),
    );
    expect(config.webSearchEnabled).toBe(true);
    expect(config.repository).toEqual({ url: 'https://github.com/org/repo', branch: 'main' });
  });

  it('collects every validation error at once', () => {
    let error: DeployConfigError | undefined;
    try {
      parseDeployConfig({
        region: 'not-a-region',
        defaultModelId: '',
        routerModelId: '',
        allowedEmailDomains: ['not a domain'],
        adminEmail: 'not-an-email',
        limits: { requestsPerSecond: -1 },
      });
    } catch (err) {
      error = err as DeployConfigError;
    }

    expect(error).toBeInstanceOf(DeployConfigError);
    expect(error!.errors.length).toBeGreaterThanOrEqual(6);
    expect(error!.errors.some((e) => e.includes('region'))).toBe(true);
    expect(error!.errors.some((e) => e.includes('defaultModelId'))).toBe(true);
    expect(error!.errors.some((e) => e.includes('routerModelId'))).toBe(true);
    expect(error!.errors.some((e) => e.includes('allowedEmailDomains'))).toBe(true);
    expect(error!.errors.some((e) => e.includes('adminEmail'))).toBe(true);
    expect(error!.errors.some((e) => e.includes('limits.requestsPerSecond'))).toBe(true);
  });

  it('rejects a config object that is not an object', () => {
    expect(() => parseDeployConfig('nope')).toThrow(DeployConfigError);
    expect(() => parseDeployConfig(null)).toThrow(DeployConfigError);
    expect(() => parseDeployConfig([])).toThrow(DeployConfigError);
  });

  it('rejects a defaultModelId that is also in blockedModelIds', () => {
    let error: DeployConfigError | undefined;
    try {
      parseDeployConfig(
        validRaw({
          defaultModelId: 'amazon.titan-text-express-v1',
          blockedModelIds: ['amazon.titan-text-express-v1'],
        }),
      );
    } catch (err) {
      error = err as DeployConfigError;
    }
    expect(error).toBeInstanceOf(DeployConfigError);
    expect(error!.errors.some((e) => e.includes('defaultModelId') && e.includes('blockedModelIds'))).toBe(true);
  });

  it('rejects a routerModelId that is also in blockedModelIds', () => {
    let error: DeployConfigError | undefined;
    try {
      parseDeployConfig(
        validRaw({
          routerModelId: 'amazon.titan-text-express-v1',
          blockedModelIds: ['amazon.titan-text-express-v1'],
        }),
      );
    } catch (err) {
      error = err as DeployConfigError;
    }
    expect(error).toBeInstanceOf(DeployConfigError);
    expect(error!.errors.some((e) => e.includes('routerModelId') && e.includes('blockedModelIds'))).toBe(true);
  });

  it('rejects a repository URL containing embedded credentials, without echoing the secret', () => {
    let error: DeployConfigError | undefined;
    try {
      parseDeployConfig(
        validRaw({ repository: { url: 'https://user:ghp_x@github.com/org/repo', branch: 'main' } }),
      );
    } catch (err) {
      error = err as DeployConfigError;
    }
    expect(error).toBeInstanceOf(DeployConfigError);
    const message = error!.errors.join('\n');
    expect(message).toContain('repository.url');
    expect(message).toContain('no credentials');
    expect(message).not.toContain('ghp_x');
    expect(error!.message).not.toContain('ghp_x');
  });

  it('rejects an ssh repository URL', () => {
    let error: DeployConfigError | undefined;
    try {
      parseDeployConfig(validRaw({ repository: { url: 'git@github.com:org/repo.git', branch: 'main' } }));
    } catch (err) {
      error = err as DeployConfigError;
    }
    expect(error).toBeInstanceOf(DeployConfigError);
    expect(error!.errors.some((e) => e.includes('repository.url'))).toBe(true);
  });

  it('accepts a well-formed https github.com repository URL with .git and a valid branch', () => {
    const config = parseDeployConfig(
      validRaw({ repository: { url: 'https://github.com/org/repo.git', branch: 'feature/my-branch' } }),
    );
    expect(config.repository).toEqual({ url: 'https://github.com/org/repo.git', branch: 'feature/my-branch' });
  });

  it('rejects a branch containing ".."', () => {
    let error: DeployConfigError | undefined;
    try {
      parseDeployConfig(validRaw({ repository: { url: 'https://github.com/org/repo', branch: '../evil' } }));
    } catch (err) {
      error = err as DeployConfigError;
    }
    expect(error).toBeInstanceOf(DeployConfigError);
    expect(error!.errors.some((e) => e.includes('repository.branch'))).toBe(true);
  });

  it('rejects a defaultModelId with an invalid format', () => {
    let error: DeployConfigError | undefined;
    try {
      parseDeployConfig(validRaw({ defaultModelId: 'bad model id!' }));
    } catch (err) {
      error = err as DeployConfigError;
    }
    expect(error).toBeInstanceOf(DeployConfigError);
    expect(error!.errors.some((e) => e.includes('defaultModelId') && e.includes('model ID format'))).toBe(true);
  });

  it('rejects an empty entry in blockedModelIds', () => {
    let error: DeployConfigError | undefined;
    try {
      parseDeployConfig(validRaw({ blockedModelIds: ['amazon.titan-text-express-v1', ''] }));
    } catch (err) {
      error = err as DeployConfigError;
    }
    expect(error).toBeInstanceOf(DeployConfigError);
    expect(error!.errors.some((e) => e.includes('blockedModelIds') && e.includes('must not be empty'))).toBe(true);
  });

  it('rejects an empty entry in allowedEmailDomains', () => {
    let error: DeployConfigError | undefined;
    try {
      parseDeployConfig(validRaw({ allowedEmailDomains: ['example.com', ''] }));
    } catch (err) {
      error = err as DeployConfigError;
    }
    expect(error).toBeInstanceOf(DeployConfigError);
    expect(error!.errors.some((e) => e.includes('allowedEmailDomains') && e.includes('must not be empty'))).toBe(true);
  });
});
