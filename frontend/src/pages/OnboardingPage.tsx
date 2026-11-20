import { useState } from 'react';
import type { FormEvent } from 'react';
import { useNavigate } from 'react-router-dom';
import { AlertTriangle, Loader2 } from 'lucide-react';
import { useAuth } from '../lib/auth/AuthProvider';
import { useProfileClient } from '../lib/useProfile';
import { ProfileClientError } from '../lib/profileClient';

// Unicode letters after NFC, matching the server's check (profile-handler _validate_name) — "José", "Zoë" must pass.
const NAME_PATTERN = /^\p{L}[\p{L}'.\- ]{0,49}$/u;
const DATE_PATTERN = /^(\d{4})-(\d{2})-(\d{2})$/;
const MIN_BIRTH_YEAR = 1900;

interface FieldErrors {
  given_name?: string;
  family_name?: string;
  birthdate?: string;
}

// Mirrors the server's own name rule (trimmed, 1-50 chars, letters/space/hyphen/apostrophe/period).
function validateName(label: string, value: string): string | undefined {
  const trimmed = value.normalize('NFC').trim();
  if (!trimmed) return `${label} is required.`;
  if (trimmed.length > 50) return `${label} must be 50 characters or fewer.`;
  if (!NAME_PATTERN.test(trimmed)) return `${label} may only contain letters, spaces, hyphens, apostrophes, and periods.`;
  return undefined;
}

// Mirrors the server's own date rule (valid calendar date, not in the future, year 1900 or later).
// The server alone decides the 18+ outcome; this only rejects obviously malformed input.
function validateBirthdate(value: string): string | undefined {
  if (!value) return 'Date of birth is required.';
  const match = DATE_PATTERN.exec(value);
  if (!match) return 'Enter a valid date.';
  const year = Number(match[1]);
  const date = new Date(`${value}T00:00:00Z`);
  if (Number.isNaN(date.getTime())) return 'Enter a valid date.';
  if (year < MIN_BIRTH_YEAR) return `Date of birth must be in ${MIN_BIRTH_YEAR} or later.`;
  const today = new Date();
  const todayUtc = Date.UTC(today.getFullYear(), today.getMonth(), today.getDate());
  if (date.getTime() > todayUtc) return 'Date of birth cannot be in the future.';
  return undefined;
}

/** Local-only estimate for the inline 18+ note; the server is the authority on age. */
function computeAge(value: string, today: Date = new Date()): number | null {
  const match = DATE_PATTERN.exec(value);
  if (!match) return null;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  let age = today.getFullYear() - year;
  const hadBirthdayThisYear = today.getMonth() + 1 > month || (today.getMonth() + 1 === month && today.getDate() >= day);
  if (!hadBirthdayThisYear) age -= 1;
  return age;
}

export function OnboardingPage() {
  const { userManager, signIn, signOut } = useAuth();
  const profileClient = useProfileClient();
  const navigate = useNavigate();

  const [givenName, setGivenName] = useState('');
  const [familyName, setFamilyName] = useState('');
  const [birthdate, setBirthdate] = useState('');
  const [errors, setErrors] = useState<FieldErrors>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [ageRestricted, setAgeRestricted] = useState(false);
  const [signingOut, setSigningOut] = useState(false);

  const birthdateFormatError = birthdate ? validateBirthdate(birthdate) : undefined;
  const age = birthdate && !birthdateFormatError ? computeAge(birthdate) : null;
  const showUnderEighteenNote = age !== null && age < 18;

  // ADR: refresh via signinSilent, not a full redirect | the new given_name/family_name/birthdate claims
  // only appear in a freshly issued ID token; a silent refresh (oidc-client-ts's refresh-token grant) gets
  // one without leaving the page, and AuthProvider's addUserLoaded listener updates `user` from it.
  async function refreshSessionAndContinue() {
    try {
      if (!userManager) throw new Error('Auth is not initialized yet.');
      await userManager.signinSilent();
      navigate('/', { replace: true });
    } catch {
      await signIn('/');
    }
  }

  async function handleCloseAccount() {
    setSigningOut(true);
    try {
      await userManager?.removeUser();
    } catch {
      // Already signed out server-side; a local cleanup failure must not block sign-out below.
    }
    try {
      await signOut();
    } catch {
      setSigningOut(false);
    }
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    const nextErrors: FieldErrors = {
      given_name: validateName('First name', givenName),
      family_name: validateName('Last name', familyName),
      birthdate: validateBirthdate(birthdate),
    };
    if (nextErrors.given_name || nextErrors.family_name || nextErrors.birthdate) {
      setErrors(nextErrors);
      return;
    }
    setErrors({});
    setFormError(null);
    setSubmitting(true);
    try {
      await profileClient.submitProfile({
        given_name: givenName.normalize('NFC').trim(),
        family_name: familyName.normalize('NFC').trim(),
        birthdate,
      });
      await refreshSessionAndContinue();
    } catch (error) {
      if (error instanceof ProfileClientError && error.code === 'profile_exists') {
        await refreshSessionAndContinue();
        return;
      }
      if (error instanceof ProfileClientError && error.code === 'age_restricted') {
        setAgeRestricted(true);
        setSubmitting(false);
        return;
      }
      setFormError(error instanceof ProfileClientError ? error.message : 'Could not save your profile. Try again.');
      setSubmitting(false);
    }
  }

  if (ageRestricted) {
    return (
      <div className="h-screen flex items-center justify-center bg-surface-primary px-4">
        <div className="max-w-md w-full rounded-xl border border-border-subtle bg-surface-elevated p-6 text-center shadow-sm">
          <AlertTriangle size={24} className="mx-auto text-evidence-weak" aria-hidden="true" />
          <h1 className="text-base font-semibold text-text-primary mt-3">Founder AI is for ages 18 and over</h1>
          <p className="text-sm text-text-secondary mt-2 leading-6">
            Founder AI is only available to people aged 18 or over. Your account has been closed.
          </p>
          <button
            type="button"
            onClick={() => void handleCloseAccount()}
            disabled={signingOut}
            className="mt-5 w-full px-4 py-2.5 rounded-lg bg-cei-blue text-white text-sm font-medium hover:bg-cei-navy transition-colors disabled:opacity-60"
          >
            {signingOut ? 'Signing out…' : 'Sign out'}
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-surface-primary flex items-center justify-center px-4 py-10">
      <div className="max-w-md w-full">
        <div className="mb-6 text-center">
          <h1 className="text-xl font-semibold text-text-primary">Finish setting up your account</h1>
          <p className="text-sm text-text-secondary mt-1.5">
            Founder AI needs your name and date of birth before you can continue.
          </p>
        </div>

        <form
          onSubmit={(event) => void handleSubmit(event)}
          noValidate
          className="rounded-xl border border-border-subtle bg-surface-elevated p-6 shadow-sm space-y-4"
        >
          <div>
            <label htmlFor="onboarding-given-name" className="block text-xs font-medium text-text-secondary mb-1">
              First name
            </label>
            <input
              id="onboarding-given-name"
              type="text"
              value={givenName}
              onChange={(event) => setGivenName(event.target.value)}
              disabled={submitting}
              autoComplete="given-name"
              aria-invalid={Boolean(errors.given_name)}
              aria-describedby={errors.given_name ? 'onboarding-given-name-error' : undefined}
              className="w-full text-sm border border-border-default rounded-md px-3 py-2 bg-white disabled:opacity-50"
            />
            {errors.given_name && (
              <p id="onboarding-given-name-error" role="alert" className="text-xs text-evidence-weak mt-1">
                {errors.given_name}
              </p>
            )}
          </div>

          <div>
            <label htmlFor="onboarding-family-name" className="block text-xs font-medium text-text-secondary mb-1">
              Last name
            </label>
            <input
              id="onboarding-family-name"
              type="text"
              value={familyName}
              onChange={(event) => setFamilyName(event.target.value)}
              disabled={submitting}
              autoComplete="family-name"
              aria-invalid={Boolean(errors.family_name)}
              aria-describedby={errors.family_name ? 'onboarding-family-name-error' : undefined}
              className="w-full text-sm border border-border-default rounded-md px-3 py-2 bg-white disabled:opacity-50"
            />
            {errors.family_name && (
              <p id="onboarding-family-name-error" role="alert" className="text-xs text-evidence-weak mt-1">
                {errors.family_name}
              </p>
            )}
          </div>

          <div>
            <label htmlFor="onboarding-birthdate" className="block text-xs font-medium text-text-secondary mb-1">
              Date of birth
            </label>
            <input
              id="onboarding-birthdate"
              type="date"
              value={birthdate}
              onChange={(event) => setBirthdate(event.target.value)}
              disabled={submitting}
              autoComplete="bday"
              aria-invalid={Boolean(errors.birthdate)}
              aria-describedby="onboarding-birthdate-hint"
              className="w-full text-sm border border-border-default rounded-md px-3 py-2 bg-white disabled:opacity-50"
            />
            <p id="onboarding-birthdate-hint" className="text-[11px] text-text-tertiary mt-1">
              Used only to confirm you are 18 or over. You can’t change this later.
            </p>
            {errors.birthdate && (
              <p role="alert" className="text-xs text-evidence-weak mt-1">
                {errors.birthdate}
              </p>
            )}
            {showUnderEighteenNote && (
              <p role="alert" className="text-xs text-evidence-weak mt-2">
                Founder AI requires founders to be 18 or over. Submitting will close your account if this
                date confirms you are under 18 — the server makes the final decision.
              </p>
            )}
          </div>

          {formError && <p role="alert" className="text-xs text-evidence-weak">{formError}</p>}

          <button
            type="submit"
            disabled={submitting}
            className="w-full px-4 py-2.5 rounded-lg bg-cei-blue text-white text-sm font-medium hover:bg-cei-navy transition-colors disabled:opacity-60 flex items-center justify-center gap-2"
          >
            {submitting ? (
              <>
                <Loader2 size={14} className="animate-spin" aria-hidden="true" /> Saving…
              </>
            ) : (
              'Continue'
            )}
          </button>
        </form>
      </div>
    </div>
  );
}
