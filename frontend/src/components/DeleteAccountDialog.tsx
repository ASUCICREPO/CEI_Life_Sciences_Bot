import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { createPortal } from 'react-dom';
import { AlertTriangle, Loader2, X } from 'lucide-react';
import { useAuth } from '../lib/auth/AuthProvider';
import { useAccountClient } from '../lib/useAccount';
import { AccountClientError } from '../lib/accountClient';

const CONFIRM_WORD = 'DELETE';

interface DeleteAccountDialogProps {
  open: boolean;
  onClose: () => void;
}

// ADR: self-contained focus trap, no shared Dialog component exists yet | Rationale: matches
// ProductTour.tsx's role="dialog" + Escape-to-close pattern, adds Tab wrap-around for full keyboard support.
export function DeleteAccountDialog({ open, onClose }: DeleteAccountDialogProps) {
  const { signOut } = useAuth();
  const accountClient = useAccountClient();
  const navigate = useNavigate();
  const [confirmText, setConfirmText] = useState('');
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) return;
    setConfirmText('');
    setError(null);
    setDeleting(false);
    const focusTimer = window.setTimeout(() => inputRef.current?.focus(), 0);
    return () => window.clearTimeout(focusTimer);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        onClose();
        return;
      }
      if (event.key !== 'Tab') return;
      const container = dialogRef.current;
      if (!container) return;
      const focusable = container.querySelectorAll<HTMLElement>('button, input, [tabindex]:not([tabindex="-1"])');
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [open, onClose]);

  if (!open) return null;

  const canConfirm = confirmText === CONFIRM_WORD && !deleting;

  async function handleConfirm() {
    if (!canConfirm) return;
    setDeleting(true);
    setError(null);
    try {
      await accountClient.deleteAccount();
      // ADR: clear every local cache before sign-out | nothing from the deleted account should survive
      // in this browser (sessionStorage chat-session ids, localStorage tour status, etc).
      sessionStorage.clear();
      localStorage.clear();
      await signOut();
      navigate('/', { replace: true });
    } catch (caught) {
      setError(caught instanceof AccountClientError ? caught.message : 'Could not delete your account. Please try again.');
      setDeleting(false);
    }
  }

  return createPortal(
    <div className="fixed inset-0 z-100 flex items-center justify-center bg-black/50 px-4">
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="delete-account-title"
        aria-describedby="delete-account-description"
        className="w-full max-w-md rounded-xl border border-border-subtle bg-surface-elevated p-6 shadow-2xl"
      >
        <div className="flex items-start justify-between gap-3 mb-3">
          <div className="flex items-center gap-2">
            <AlertTriangle size={18} className="text-evidence-weak shrink-0" aria-hidden="true" />
            <h2 id="delete-account-title" className="text-sm font-semibold text-text-primary">Delete account</h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={deleting}
            className="p-1 text-text-tertiary hover:text-text-primary rounded disabled:opacity-50"
            aria-label="Cancel"
          >
            <X size={16} />
          </button>
        </div>

        <p id="delete-account-description" className="text-sm text-text-secondary leading-6">
          Your account is disabled at once, and you are signed out. All chats, files, memories and API keys
          are deleted after about 9 hours. This can not be undone.
        </p>

        <label htmlFor="delete-account-confirm" className="block text-xs font-medium text-text-secondary mt-4 mb-1">
          Type DELETE to confirm
        </label>
        <input
          id="delete-account-confirm"
          ref={inputRef}
          type="text"
          value={confirmText}
          onChange={(event) => setConfirmText(event.target.value)}
          disabled={deleting}
          autoComplete="off"
          className="w-full text-sm border border-border-default rounded-md px-2 py-1.5 bg-white disabled:opacity-50"
        />

        {error && <p role="alert" className="mt-3 text-xs text-evidence-weak">{error}</p>}

        <div className="flex justify-end gap-2 mt-5">
          <button
            type="button"
            onClick={onClose}
            disabled={deleting}
            className="text-xs font-medium px-3 py-1.5 rounded-md border border-border-default text-text-secondary disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => void handleConfirm()}
            disabled={!canConfirm}
            className="text-xs font-medium px-3 py-1.5 rounded-md bg-evidence-weak text-white disabled:opacity-50 flex items-center gap-1.5"
          >
            {deleting ? (
              <>
                <Loader2 size={12} className="animate-spin" aria-hidden="true" /> Deleting…
              </>
            ) : (
              'Delete my account'
            )}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
