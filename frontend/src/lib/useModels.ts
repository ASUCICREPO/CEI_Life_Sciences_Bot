import { useMemo } from 'react';
import { useAuth } from './auth/AuthProvider';
import { createModelsClient, type ModelsClient } from './modelsClient';

/**
 * One ModelsClient per access-token provider and API base; every call carries the caller's Cognito token.
 * Mirrors useSessionsClient (useSessions.ts): the base comes from /config.json `apiBaseUrl`.
 */
export function useModelsClient(): ModelsClient {
  const { getAccessToken, config } = useAuth();
  const baseUrl = config?.apiBaseUrl;
  return useMemo(() => createModelsClient(getAccessToken, { baseUrl }), [getAccessToken, baseUrl]);
}
