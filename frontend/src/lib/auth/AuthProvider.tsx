import { createContext, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useState, type ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';
import type { User, UserManager } from 'oidc-client-ts';
import { loadRuntimeConfig, type RuntimeConfig } from './config';
import { getUserManager, buildCognitoLogoutUrl } from './userManager';
import { setAgentTokenProvider } from '../agentClient';
import { configureClientDiagnostics, installGlobalDiagnostics } from '../clientDiagnostics';
import { setOnboardingRequiredHandler } from './onboardingGate';

interface AuthContextValue {
  user: User | null;
  isLoading: boolean;
  error: Error | null;
  userManager: UserManager | null;
  signIn: (returnTo?: string) => Promise<void>;
  signOut: () => Promise<void>;
  getAccessToken: () => Promise<string | null>;
  /** The validated /config.json; null until it has loaded. */
  config: RuntimeConfig | null;
}

const AuthContext = createContext<AuthContextValue | null>(null);

// ADR: no auth bypass in any build | local dev signs in to a real dev Cognito pool exactly like prod,
// so every API call carries a real access token and the backend scopes data by the token's `sub`.
export function AuthProvider({ children }: { children: ReactNode }) {
  const navigate = useNavigate();
  const [userManager, setUserManager] = useState<UserManager | null>(null);
  const [config, setConfig] = useState<RuntimeConfig | null>(null);
  const [user, setUser] = useState<User | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);

  useEffect(() => {
    let cancelled = false;

    loadRuntimeConfig()
      .then((loadedConfig) => {
        if (cancelled) return;
        const manager = getUserManager(loadedConfig);
        setConfig(loadedConfig);
        setUserManager(manager);
        return manager.getUser();
      })
      .then((loadedUser) => {
        if (cancelled) return;
        setUser(loadedUser ?? null);
      })
      .catch((loadError: unknown) => {
        if (cancelled) return;
        setError(loadError instanceof Error ? loadError : new Error('Failed to initialize auth.'));
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!userManager) return;

    const onUserLoaded = (loadedUser: User) => setUser(loadedUser);
    const onUserUnloaded = () => { configureClientDiagnostics(null); setUser(null); };
    const onAccessTokenExpired = () => { configureClientDiagnostics(null); setUser(null); };

    userManager.events.addUserLoaded(onUserLoaded);
    userManager.events.addUserUnloaded(onUserUnloaded);
    userManager.events.addAccessTokenExpired(onAccessTokenExpired);

    return () => {
      userManager.events.removeUserLoaded(onUserLoaded);
      userManager.events.removeUserUnloaded(onUserUnloaded);
      userManager.events.removeAccessTokenExpired(onAccessTokenExpired);
    };
  }, [userManager]);

  const signIn = useCallback(
    async (returnTo?: string) => {
      if (!userManager) throw new Error('Auth is not initialized yet.');
      await userManager.signinRedirect({ state: { returnTo: returnTo ?? window.location.pathname } });
    },
    [userManager]
  );

  const signOut = useCallback(async () => {
    if (!userManager || !config) throw new Error('Auth is not initialized yet.');
    try {
      await userManager.revokeTokens(['refresh_token']);
    } catch {
      // Revocation failure must not block sign-out; the local session is still cleared.
    }
    configureClientDiagnostics(null);
    await userManager.removeUser();
    window.location.href = buildCognitoLogoutUrl(config);
  }, [userManager, config]);

  const getAccessToken = useCallback(async () => {
    if (!userManager) return null;
    const currentUser = await userManager.getUser();
    if (!currentUser || currentUser.expired) return null;
    return currentUser.access_token ?? null;
  }, [userManager]);

  // Agent calls carry the access token so runtimes record history under the caller's Cognito sub
  useEffect(() => {
    setAgentTokenProvider(getAccessToken);
    return () => setAgentTokenProvider(null);
  }, [getAccessToken]);

  // ADR: register the onboarding redirect here | the non-React clients (agentClient, sessionsClient,
  // openStream) hit a run-starting 403 onboarding_required and call this instead of importing the router.
  useEffect(() => {
    setOnboardingRequiredHandler(() => navigate('/onboarding', { replace: true }));
    return () => setOnboardingRequiredHandler(null);
  }, [navigate]);

  const authenticated = Boolean(user && !user.expired);
  useLayoutEffect(() => {
    if (!config || !authenticated) return;
    configureClientDiagnostics({ baseUrl: config.apiBaseUrl ?? '/api', getToken: getAccessToken });
    const remove = installGlobalDiagnostics();
    return () => { remove(); configureClientDiagnostics(null); };
  }, [config, getAccessToken, authenticated]);

  const value = useMemo<AuthContextValue>(
    () => ({ user, isLoading, error, userManager, config, signIn, signOut, getAccessToken }),
    [user, isLoading, error, userManager, config, signIn, signOut, getAccessToken]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error('useAuth must be used within an AuthProvider.');
  }
  return context;
}
