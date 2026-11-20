'use strict';

/**
 * B112 run-start/write gate: the `cei_age_verified` access-token claim (pre-token-generation/handler.py)
 * is `"true"` only when the signed-in user's stored `birthdate` is present, valid, and the user is 18+
 * today (UTC). Shared by every Node Lambda that starts a new run or writes user data (Session Runner,
 * UiActions), the same way `account-gate.js` is shared. The Python equivalent is
 * `http_errors.require_age_verified` (backend/lambdas/shared/http_errors.py), used by the Python REST
 * Lambdas. Unlike the account-deleting gate, this is a pure function of the request's own claims -- no
 * DynamoDB read is needed.
 */

const AGE_VERIFIED_CLAIM = 'cei_age_verified';

/**
 * @param {unknown} event - the API Gateway proxy event (Cognito authorizer claims on requestContext).
 * @returns {boolean}
 */
function isAgeVerified(event) {
  const claims = event?.requestContext?.authorizer?.claims;
  return !!claims && claims[AGE_VERIFIED_CLAIM] === 'true';
}

module.exports = { isAgeVerified, AGE_VERIFIED_CLAIM };
