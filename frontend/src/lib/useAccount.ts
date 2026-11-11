import { useMemo } from 'react';
import { useAuth } from './auth/AuthProvider';
import { createAccountClient, type AccountClient } from './accountClient';

/**
 * One AccountClient per access-token provider and API base; every call carries the caller's Cognito token.
 * Mirrors useModelsClient (useModels.ts): the base comes from /config.json `apiBaseUrl`.
 */
export function useAccountClient(): AccountClient {
  const { getAccessToken, config } = useAuth();
  const baseUrl = config?.apiBaseUrl;
  return useMemo(() => createAccountClient(getAccessToken, { baseUrl }), [getAccessToken, baseUrl]);
}
