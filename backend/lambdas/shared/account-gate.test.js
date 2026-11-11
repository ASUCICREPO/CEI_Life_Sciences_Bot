'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { isAccountDeleting, RECORD_KEY } = require('./account-gate');

// A fake stand-in for @aws-sdk/client-dynamodb's GetItemCommand (provided by the Lambda Node.js runtime,
// not a repo dependency -- see the real callers in session-runner/index.js and ui-actions/index.js).
class FakeGetItemCommand {
  constructor(input) { this.input = input; }
}

test('isAccountDeleting returns false without a table (flag off, never blocks)', async () => {
  const db = { send: async () => { throw new Error('must not be called'); } };
  assert.equal(await isAccountDeleting(db, '', 'user-1', FakeGetItemCommand), false);
  assert.equal(await isAccountDeleting(db, undefined, 'user-1', FakeGetItemCommand), false);
});

test('isAccountDeleting reads the deletion record with a consistent GetItem', async () => {
  let captured;
  const db = { send: async (command) => { captured = command.input; return { Item: { user_id: { S: 'user-1' }, record: { S: RECORD_KEY } } }; } };
  const result = await isAccountDeleting(db, 'UserMetadataTable', 'user-1', FakeGetItemCommand);
  assert.equal(result, true);
  assert.equal(captured.TableName, 'UserMetadataTable');
  assert.deepEqual(captured.Key, { user_id: { S: 'user-1' }, record: { S: 'deletion' } });
  assert.equal(captured.ConsistentRead, true);
});

test('isAccountDeleting returns false when no row exists', async () => {
  const db = { send: async () => ({}) };
  assert.equal(await isAccountDeleting(db, 'UserMetadataTable', 'user-1', FakeGetItemCommand), false);
});
