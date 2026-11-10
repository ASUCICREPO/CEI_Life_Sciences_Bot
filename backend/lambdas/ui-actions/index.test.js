'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { createHandler, validateRequest } = require('./index');

const SESSION = '6f1c2d3e-0000-4000-8000-000000000001';
const ACTION = { request_id: 'r1', session_id: SESSION, surface_id: 'analysis-scope', revision: 1, interaction_id: 'i-1', component_id: 'c-1', action_id: 'approve-scope', value: null };

function request(body = ACTION, { sub = 'user-1', auth = 'Bearer token' } = {}) {
  return { requestContext: { requestId: 'req-1', authorizer: { claims: { sub } } }, headers: { Authorization: auth }, body: JSON.stringify(body) };
}

function harness({ agent = 'analysis', envelope = { status: 'completed', text: 'done' }, fail } = {}) {
  const calls = [];
  const out = { chunks: [], metadata: null, ended: false };
  const deps = {
    headers: () => ({ 'Content-Type': 'application/json' }),
    sessionAgent: async (user, session) => (user === 'user-1' && session === SESSION ? agent : null),
    resume: async (target, parsed) => { calls.push({ target, parsed }); if (fail) throw fail; return envelope; },
  };
  const responseFrom = (_raw, metadata) => {
    out.metadata = metadata;
    return { write: (chunk) => out.chunks.push(chunk), end: () => { out.ended = true; } };
  };
  return { handler: createHandler(responseFrom, deps), calls, out, body: () => JSON.parse(out.chunks.join('')) };
}

test('a valid action on an owned analysis chat resumes the analysis runtime with the caller token', async () => {
  const h = harness();
  await h.handler(request(), {});
  assert.equal(h.out.metadata.statusCode, 200);
  assert.deepEqual(h.body(), { status: 'completed', text: 'done' });
  assert.equal(h.calls[0].target, 'analysis');
  assert.equal(h.calls[0].parsed.bearerToken, 'token');
  assert.deepEqual(h.calls[0].parsed.action, ACTION);
  assert.ok(h.out.ended);
});

test('pipeline chats resume the pipeline runtime', async () => {
  const h = harness({ agent: 'pipeline_manager' });
  await h.handler(request(), {});
  assert.equal(h.calls[0].target, 'pipeline');
});

test('another user\'s session is not found and never reaches a runtime', async () => {
  const h = harness();
  await h.handler(request(ACTION, { sub: 'user-2' }), {});
  assert.equal(h.out.metadata.statusCode, 404);
  assert.equal(h.body().error.code, 'not_found');
  assert.equal(h.calls.length, 0);
});

test('chats whose agent cannot pause are refused', async () => {
  for (const agent of ['research', '', 'unknown']) {
    const h = harness({ agent });
    await h.handler(request(), {});
    assert.equal(h.out.metadata.statusCode, 400);
    assert.equal(h.calls.length, 0);
  }
});

test('stale and expired answers from the runtime pass through unchanged', async () => {
  const expired = { status: 'rejected', code: 'expired', action_response: { status: 'rejected', message: 'This request has expired. Start again.' } };
  const h = harness({ envelope: expired });
  await h.handler(request(), {});
  assert.equal(h.out.metadata.statusCode, 200);
  assert.deepEqual(h.body(), expired);
});

test('a runtime failure is a retriable 503 without internal detail', async () => {
  const h = harness({ fail: new Error('upstream body with secrets') });
  await h.handler(request(), {});
  assert.equal(h.out.metadata.statusCode, 503);
  assert.equal(h.body().error.retriable, true);
  assert.ok(!h.out.chunks.join('').includes('secrets'));
});

test('a malformed runtime envelope is treated as a failure', async () => {
  const h = harness({ envelope: { status: 'surprise' } });
  await h.handler(request(), {});
  assert.equal(h.out.metadata.statusCode, 503);
});

test('validation rejects missing auth, unknown or missing fields and bad bounds', () => {
  assert.throws(() => validateRequest(request(ACTION, { auth: '' })), { statusCode: 401 });
  assert.throws(() => validateRequest({ ...request(), requestContext: {} }), { statusCode: 401 });
  for (const bad of [
    { ...ACTION, extra: 1 },
    { ...ACTION, session_id: 'not-a-uuid' },
    { ...ACTION, revision: 0 },
    { ...ACTION, revision: 1.5 },
    { ...ACTION, action_id: '' },
    { ...ACTION, surface_id: 'x'.repeat(129) },
    (() => { const { action_id, ...rest } = ACTION; return rest; })(),
    [ACTION],
  ]) {
    assert.throws(() => validateRequest(request(bad)), { statusCode: 400 }, JSON.stringify(bad).slice(0, 80));
  }
  assert.throws(() => validateRequest({ ...request(), body: 'x'.repeat(33 * 1024) }), { statusCode: 400 });
  assert.throws(() => validateRequest({ ...request(), body: '{' }), { statusCode: 400 });
});

test('a missing value is sent as null', () => {
  const { value, ...rest } = ACTION;
  assert.equal(validateRequest(request(rest)).action.value, null);
});
