import type { IdTokenClaims } from 'oidc-client-ts';

type NameClaims = Pick<IdTokenClaims, 'given_name' | 'name' | 'email'>;
type FullNameClaims = Pick<IdTokenClaims, 'given_name' | 'family_name' | 'name' | 'email'>;

function titleCase(word: string): string {
  return word.charAt(0).toUpperCase() + word.slice(1).toLowerCase();
}

/**
 * First name to greet the signed-in user with. The user pool collects only email today, so
 * `given_name` / `name` are used when present and the email local part is the fallback
 * (`jane.doe@x.com` -> `Jane`). Returns null when no usable claim exists.
 */
export function displayFirstName(claims: NameClaims | null | undefined): string | null {
  if (!claims) return null;

  const given = claims.given_name?.trim();
  if (given) return given;

  const full = claims.name?.trim();
  if (full) return full.split(/\s+/)[0];

  const local = claims.email?.split('@')[0]?.trim();
  if (!local) return null;
  const first = local.split(/[._+\-\d]+/).find((part) => part.length > 0);
  return first ? titleCase(first) : null;
}

/** Full name once the onboarding profile (given_name + family_name) is set; falls back to displayFirstName. */
export function displayFullName(claims: FullNameClaims | null | undefined): string | null {
  if (!claims) return null;

  const given = claims.given_name?.trim();
  const family = claims.family_name?.trim();
  if (given && family) return `${given} ${family}`;

  return displayFirstName(claims);
}

/** Greeting for the local hour of `now`. */
export function timeOfDayGreeting(now: Date = new Date()): string {
  const hour = now.getHours();
  if (hour < 12) return 'Good morning';
  if (hour < 18) return 'Good afternoon';
  return 'Good evening';
}
