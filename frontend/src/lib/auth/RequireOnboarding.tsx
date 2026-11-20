import type { ReactNode } from 'react';
import { Navigate, Outlet } from 'react-router-dom';
import { useAuth } from './AuthProvider';
import { isProfileComplete } from './profileCompleteness';

/** Gate for every route except /onboarding itself: an incomplete profile is sent to /onboarding. */
export function RequireOnboarding({ children }: { children?: ReactNode }) {
  const { user } = useAuth();
  if (!isProfileComplete(user?.profile)) return <Navigate to="/onboarding" replace />;
  return <>{children ?? <Outlet />}</>;
}

/** Gate for the /onboarding route: a completed profile is sent back to /. */
export function RequireIncompleteProfile({ children }: { children?: ReactNode }) {
  const { user } = useAuth();
  if (isProfileComplete(user?.profile)) return <Navigate to="/" replace />;
  return <>{children ?? <Outlet />}</>;
}
