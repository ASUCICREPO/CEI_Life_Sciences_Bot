import type { IdTokenClaims } from 'oidc-client-ts';

export type ProfileClaims = Pick<IdTokenClaims, 'given_name' | 'family_name' | 'birthdate'>;

function nonEmpty(value: unknown): boolean {
  return typeof value === 'string' && value.trim().length > 0;
}

/** True once Cognito's given_name/family_name/birthdate standard attributes are all set on the ID token. */
export function isProfileComplete(claims: ProfileClaims | null | undefined): boolean {
  if (!claims) return false;
  return nonEmpty(claims.given_name) && nonEmpty(claims.family_name) && nonEmpty(claims.birthdate);
}
