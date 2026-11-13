import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { AlertTriangle } from 'lucide-react';
import { useAuth } from './AuthProvider';

export function CallbackPage() {
  const { userManager, signIn } = useAuth();
  const navigate = useNavigate();
  const [error, setError] = useState<Error | null>(null);
  const hasRun = useRef(false);

  useEffect(() => {
    if (!userManager || hasRun.current) return;
    hasRun.current = true;

    userManager
      .signinRedirectCallback()
      .then((completedUser) => {
        const requested = (completedUser.state as { returnTo?: string } | undefined)?.returnTo;
        const isAppPath = typeof requested === 'string' && requested.startsWith('/') && !requested.startsWith('//');
        navigate(isAppPath ? requested : '/', { replace: true });
      })
      .catch((callbackError: unknown) => {
        setError(callbackError instanceof Error ? callbackError : new Error('Sign-in failed.'));
      });
  }, [userManager, navigate]);

  if (error) {
    return (
      <div className="h-screen flex items-center justify-center bg-surface-primary">
        <div className="max-w-sm text-center space-y-3">
          <AlertTriangle size={24} className="mx-auto text-review-required" aria-hidden="true" />
          <p className="text-sm text-text-primary font-medium">Sign-in did not complete.</p>
          <p className="text-xs text-text-secondary">{error.message}</p>
          <button
            type="button"
            onClick={() => void signIn('/')}
            className="text-sm text-cei-blue font-medium hover:underline"
          >
            Try again
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="h-screen flex items-center justify-center bg-surface-primary">
      <p className="text-sm text-text-secondary">Completing sign-in…</p>
    </div>
  );
}
