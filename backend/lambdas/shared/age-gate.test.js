'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { isAgeVerified, AGE_VERIFIED_CLAIM } = require('./age-gate');

test('isAgeVerified is true only for the exact string "true"', () => {
  assert.equal(isAgeVerified({ requestContext: { authorizer: { claims: { [AGE_VERIFIED_CLAIM]: 'true' } } } }), true);
});

test('isAgeVerified is false when the claim is missing, false, or the wrong type', () => {
  assert.equal(isAgeVerified({ requestContext: { authorizer: { claims: {} } } }), false);
  assert.equal(isAgeVerified({ requestContext: { authorizer: { claims: { [AGE_VERIFIED_CLAIM]: 'false' } } } }), false);
  assert.equal(isAgeVerified({ requestContext: { authorizer: { claims: { [AGE_VERIFIED_CLAIM]: true } } } }), false);
});

test('isAgeVerified is false when there is no authorizer or claims at all', () => {
  assert.equal(isAgeVerified({}), false);
  assert.equal(isAgeVerified({ requestContext: {} }), false);
  assert.equal(isAgeVerified({ requestContext: { authorizer: {} } }), false);
  assert.equal(isAgeVerified(undefined), false);
});
