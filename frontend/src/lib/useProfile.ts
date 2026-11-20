import { useMemo } from 'react';
import { useAuth } from './auth/AuthProvider';
import { createProfileClient, type ProfileClient } from './profileClient';

/**
 * One ProfileClient per access-token provider and API base; every call carries the caller's Cognito token.
 * Mirrors useAccountClient (useAccount.ts): the base comes from /config.json `apiBaseUrl`.
 */
export function useProfileClient(): ProfileClient {
  const { getAccessToken, config } = useAuth();
  const baseUrl = config?.apiBaseUrl;
  return useMemo(() => createProfileClient(getAccessToken, { baseUrl }), [getAccessToken, baseUrl]);
}
