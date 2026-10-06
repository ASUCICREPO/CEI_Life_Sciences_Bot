import { useEffect, type ReactNode } from 'react';
import { Outlet } from 'react-router-dom';
import { Loader2 } from 'lucide-react';
import { useAuth } from './AuthProvider';

export function RequireAuth({ children }: { children?: ReactNode }) {
  const { user, isLoading, signIn } = useAuth();
  const isLocalDemo = import.meta.env.DEV && import.meta.env.VITE_LOCAL_DEMO === 'true';

  useEffect(() => {
    if (isLocalDemo) return;
    if (!isLoading && !user) {
      void signIn(window.location.pathname);
    }
  }, [isLocalDemo, isLoading, user, signIn]);

  if (isLocalDemo) return <>{children ?? <Outlet />}</>;

  if (isLoading || !user) {
    return (
      <div className="h-screen flex items-center justify-center bg-surface-primary">
        <div className="flex items-center gap-2 text-sm text-text-secondary">
          <Loader2 size={16} className="animate-spin text-cei-blue" aria-hidden="true" />
          <span>Signing you in…</span>
        </div>
      </div>
    );
  }

  return <>{children ?? <Outlet />}</>;
}
