import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import type { User, UserManager } from 'oidc-client-ts';
import { loadRuntimeConfig, type RuntimeConfig } from './config';
import { getUserManager, buildCognitoLogoutUrl } from './userManager';

interface AuthContextValue {
  user: User | null;
  isLoading: boolean;
  error: Error | null;
  userManager: UserManager | null;
  signIn: (returnTo?: string) => Promise<void>;
  signOut: () => Promise<void>;
  getAccessToken: () => Promise<string | null>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
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
    const onUserUnloaded = () => setUser(null);
    const onAccessTokenExpired = () => setUser(null);

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
    await userManager.removeUser();
    window.location.href = buildCognitoLogoutUrl(config);
  }, [userManager, config]);

  const getAccessToken = useCallback(async () => {
    if (!userManager) return null;
    const currentUser = await userManager.getUser();
    return currentUser?.access_token ?? null;
  }, [userManager]);

  const value = useMemo<AuthContextValue>(
    () => ({ user, isLoading, error, userManager, signIn, signOut, getAccessToken }),
    [user, isLoading, error, userManager, signIn, signOut, getAccessToken]
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
