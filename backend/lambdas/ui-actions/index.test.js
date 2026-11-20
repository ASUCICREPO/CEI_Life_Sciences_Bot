'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { createHandler, validateRequest, isRouteChoice, actionResponse, mapRouterError, agentRunBodyFor, wantsEventStream, ROUTE_CHOICE_INTERACTION_ID, ROUTE_CHOICE_COMPONENT_ID, ROUTE_CHOICE_ACTION_ID } = require('./index');

const SESSION = '6f1c2d3e-0000-4000-8000-000000000001';
const ACTION = { request_id: 'r1', session_id: SESSION, surface_id: 'analysis-scope', revision: 1, interaction_id: 'i-1', component_id: 'c-1', action_id: 'approve-scope', value: null };

function request(body = ACTION, { sub = 'user-1', auth = 'Bearer token', accept, ageVerified = 'true' } = {}) {
  return { requestContext: { requestId: 'req-1', authorizer: { claims: { sub, cei_age_verified: ageVerified } } }, headers: { Authorization: auth, ...(accept ? { Accept: accept } : {}) }, body: JSON.stringify(body) };
}

function harness({ agent = 'analysis', envelope = { status: 'completed', text: 'done' }, fail, routeChoice = {}, deleting = false, streamChunks, streamFail } = {}) {
  const calls = [];
  const out = { chunks: [], metadata: null, ended: false };
  const deps = {
    headers: (_event, sse) => ({ 'Content-Type': sse ? 'text/event-stream' : 'application/json' }),
    accountDeleting: async () => deleting,
    sessionAgent: async (user, session) => (user === 'user-1' && session === SESSION ? agent : null),
    resume: async (target, parsed) => { calls.push({ target, parsed }); if (fail) throw fail; return envelope; },
    // live-drafting: streamed drafting resume only (gated on Accept: text/event-stream in the request).
    resumeStream: async (target, parsed) => {
      calls.push({ target: `${target}Stream`, parsed });
      if (streamFail) throw streamFail;
      const chunks = streamChunks ?? [Buffer.from('id: 0\nevent: surface_begin\ndata: {}\n\nid: 1\nevent: surface_end\ndata: {}\n\n')];
      return (async function* () { for (const chunk of chunks) yield chunk; })();
    },
    // F9: route-choice-only deps; each test below overrides only what it exercises.
    newRunId: () => 'new-run-id',
    pendingChoice: async () => routeChoice.pending ?? null,
    clearPendingChoice: async (userId, sessionId) => calls.push({ target: 'clearPendingChoice', userId, sessionId }),
    routeStart: async (bearerToken, runId, sessionId, chosenAgent, pending, requestId) => {
      calls.push({ target: 'routeStart', bearerToken, runId, sessionId, chosenAgent, pending, requestId });
      const value = routeChoice.route ?? { status: 200, runtime_url: 'https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/x/invocations?qualifier=DEFAULT', gateway_token: 'secret' };
      // Mirrors productionDependencies().routeStart's own contract: throw on anything but a routed 200.
      if (value.status !== 200 || !value.runtime_url) throw mapRouterError(value);
      return value;
    },
    routeFinish: async (bearerToken, sessionId, runId) => calls.push({ target: 'routeFinish', bearerToken, sessionId, runId }),
    invokeAgentRun: async (route, chosenAgent, pending, parsed) => {
      calls.push({ target: 'invokeAgentRun', route, chosenAgent, pending, parsed });
      if (routeChoice.fail) throw routeChoice.fail;
      return routeChoice.envelope ?? { status: 'completed', text: 'done' };
    },
  };
  const responseFrom = (_raw, metadata) => {
    out.metadata = metadata;
    return { write: (chunk) => out.chunks.push(chunk), end: () => { out.ended = true; } };
  };
  return { handler: createHandler(responseFrom, deps), calls, out, body: () => JSON.parse(out.chunks.join('')) };
}

const routeChoiceAction = (overrides = {}) => ({
  request_id: 'r1', session_id: SESSION, surface_id: 'router-choice-1', revision: 1,
  interaction_id: ROUTE_CHOICE_INTERACTION_ID, component_id: ROUTE_CHOICE_COMPONENT_ID, action_id: ROUTE_CHOICE_ACTION_ID,
  value: 'drafting', ...overrides,
});

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

test('research chats resume the research runtime', async () => {
  const h = harness({ agent: 'research' });
  await h.handler(request(), {});
  assert.equal(h.calls[0].target, 'research');
});

test('drafting chats resume the drafting runtime', async () => {
  const h = harness({ agent: 'drafting' });
  await h.handler(request(), {});
  assert.equal(h.calls[0].target, 'drafting');
});

// live-drafting: a drafting resume streams only when the client sends Accept: text/event-stream.
test('a drafting resume with Accept: text/event-stream pipes SSE bytes through unchanged', async () => {
  const h = harness({ agent: 'drafting' });
  await h.handler(request(ACTION, { accept: 'text/event-stream' }), {});
  assert.equal(h.calls[0].target, 'draftingStream');
  assert.equal(h.out.metadata.statusCode, 200);
  assert.equal(h.out.metadata.headers['Content-Type'], 'text/event-stream');
  assert.equal(h.out.chunks.join(''), 'id: 0\nevent: surface_begin\ndata: {}\n\nid: 1\nevent: surface_end\ndata: {}\n\n');
  assert.ok(h.out.ended);
});

test('a drafting resume without Accept: text/event-stream still returns one buffered JSON body', async () => {
  const h = harness({ agent: 'drafting' });
  await h.handler(request(), {});
  assert.equal(h.calls[0].target, 'drafting');
  assert.equal(h.out.metadata.headers['Content-Type'], 'application/json');
  assert.deepEqual(h.body(), { status: 'completed', text: 'done' });
});

test('a non-drafting resume ignores Accept: text/event-stream and stays buffered', async () => {
  const h = harness({ agent: 'analysis' });
  await h.handler(request(ACTION, { accept: 'text/event-stream' }), {});
  assert.equal(h.calls[0].target, 'analysis');
  assert.equal(h.out.metadata.headers['Content-Type'], 'application/json');
});

test('a failed drafting stream call is a retriable 503 before any bytes are sent', async () => {
  const h = harness({ agent: 'drafting', streamFail: new Error('upstream body with secrets') });
  await h.handler(request(ACTION, { accept: 'text/event-stream' }), {});
  assert.equal(h.out.metadata.statusCode, 503);
  assert.equal(h.body().error.retriable, true);
  assert.ok(!h.out.chunks.join('').includes('secrets'));
});

test('B109: a deleting account cannot stream a drafting resume either', async () => {
  const h = harness({ agent: 'drafting', deleting: true });
  await h.handler(request(ACTION, { accept: 'text/event-stream' }), {});
  assert.equal(h.out.metadata.statusCode, 403);
  assert.equal(h.calls.length, 0);
});

test('wantsEventStream matches Accept headers case-insensitively, including a header-name case variant', () => {
  assert.equal(wantsEventStream({ headers: { Accept: 'text/event-stream' } }), true);
  assert.equal(wantsEventStream({ headers: { accept: 'TEXT/EVENT-STREAM' } }), true);
  assert.equal(wantsEventStream({ headers: { Accept: 'application/json' } }), false);
  assert.equal(wantsEventStream({ headers: {} }), false);
});

test('agentRunBodyFor sends no document_type for a free-text route-choice pick', () => {
  assert.deepEqual(agentRunBodyFor({ message: 'hello', mode: 'science' }), { prompt: 'hello', mode: 'science' });
});

test('another user\'s session is not found and never reaches a runtime', async () => {
  const h = harness();
  await h.handler(request(ACTION, { sub: 'user-2' }), {});
  assert.equal(h.out.metadata.statusCode, 404);
  assert.equal(h.body().error.code, 'not_found');
  assert.equal(h.calls.length, 0);
});

test('chats whose agent cannot pause are refused', async () => {
  for (const agent of ['tool_agent', '', 'unknown']) {
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

// F9/B80: the route-choice action -- a ChoicePicker pick, not a graph resume.
test('isRouteChoice matches only the fixed router-choice action and not an ordinary resume', () => {
  assert.equal(isRouteChoice(routeChoiceAction()), true);
  assert.equal(isRouteChoice(ACTION), false);
});

test('a valid route choice starts the chosen agent through the Router and returns one envelope', async () => {
  const future = Math.floor(Date.now() / 1000) + 300;
  const pending = { message: 'original free-text prompt', mode: 'science', surface_id: 'router-choice-1', expires_at: future };
  const h = harness({ routeChoice: { pending, envelope: { status: 'completed', text: 'done', ui: { root_id: 'root' } } } });
  await h.handler(request(routeChoiceAction()), {});
  assert.equal(h.out.metadata.statusCode, 200);
  const body = h.body();
  assert.equal(body.status, 'completed');
  assert.equal(body.action_response.status, 'completed');
  assert.equal(body.action_response.action_id, ROUTE_CHOICE_ACTION_ID);
  const routeStart = h.calls.find((c) => c.target === 'routeStart');
  assert.equal(routeStart.chosenAgent, 'drafting');
  assert.equal(routeStart.pending.message, 'original free-text prompt');
  assert.ok(h.calls.some((c) => c.target === 'clearPendingChoice'));
  assert.ok(h.calls.some((c) => c.target === 'routeFinish' && c.runId === 'new-run-id'));
});

test('an expired route choice is rejected without starting the Router', async () => {
  const pending = { message: 'x', mode: 'science', surface_id: 'router-choice-1', expires_at: Math.floor(Date.now() / 1000) - 10 };
  const h = harness({ routeChoice: { pending } });
  await h.handler(request(routeChoiceAction()), {});
  assert.equal(h.out.metadata.statusCode, 200);
  const body = h.body();
  assert.equal(body.status, 'rejected'); assert.equal(body.code, 'expired');
  assert.equal(h.calls.some((c) => c.target === 'routeStart'), false);
});

test('a route choice with no pending record is treated as expired', async () => {
  const h = harness({ routeChoice: { pending: null } });
  await h.handler(request(routeChoiceAction()), {});
  const body = h.body();
  assert.equal(body.status, 'rejected'); assert.equal(body.code, 'expired');
});

test('a route choice whose surface_id does not match the pending record is stale', async () => {
  const future = Math.floor(Date.now() / 1000) + 300;
  const pending = { message: 'x', mode: 'science', surface_id: 'router-choice-OTHER', expires_at: future };
  const h = harness({ routeChoice: { pending } });
  await h.handler(request(routeChoiceAction()), {});
  const body = h.body();
  assert.equal(body.status, 'stale');
  assert.equal(h.calls.some((c) => c.target === 'routeStart'), false);
});

test('a route choice value outside the four agents is stale, not trusted as a destination', async () => {
  const future = Math.floor(Date.now() / 1000) + 300;
  const pending = { message: 'x', mode: 'science', surface_id: 'router-choice-1', expires_at: future };
  const h = harness({ routeChoice: { pending } });
  await h.handler(request(routeChoiceAction({ value: 'not-a-real-agent' })), {});
  assert.equal(h.body().status, 'stale');
});

test('a foreign session never reaches the pending choice or the Router', async () => {
  const h = harness();
  await h.handler(request(routeChoiceAction(), { sub: 'user-2' }), {});
  assert.equal(h.out.metadata.statusCode, 404);
  assert.equal(h.calls.length, 0);
});

test('a concurrency-limited Router start maps to a 429, not a silent 200', async () => {
  const future = Math.floor(Date.now() / 1000) + 300;
  const pending = { message: 'x', mode: 'science', surface_id: 'router-choice-1', expires_at: future };
  const h = harness({ routeChoice: { pending, route: { status: 429, code: 'concurrency_limit' } } });
  await h.handler(request(routeChoiceAction()), {});
  assert.equal(h.out.metadata.statusCode, 429);
  assert.equal(h.body().error.code, 'concurrency_limit');
});

test('the Router lease is always released even when the agent invocation fails', async () => {
  const future = Math.floor(Date.now() / 1000) + 300;
  const pending = { message: 'x', mode: 'science', surface_id: 'router-choice-1', expires_at: future };
  const h = harness({ routeChoice: { pending, fail: new Error('runtime unavailable with secrets') } });
  await h.handler(request(routeChoiceAction()), {});
  assert.equal(h.out.metadata.statusCode, 503);
  assert.ok(!h.out.chunks.join('').includes('secrets'));
  assert.ok(h.calls.some((c) => c.target === 'routeFinish'));
  assert.equal(h.calls.some((c) => c.target === 'clearPendingChoice'), false);
});

test('B109: a deleting account cannot resume a paused run', async () => {
  const h = harness({ deleting: true });
  await h.handler(request(), {});
  assert.equal(h.out.metadata.statusCode, 403);
  assert.equal(h.body().error.code, 'forbidden');
  assert.equal(h.calls.length, 0);
});

test('B109: a deleting account cannot start a run through a route choice either', async () => {
  const future = Math.floor(Date.now() / 1000) + 300;
  const pending = { message: 'x', mode: 'science', surface_id: 'router-choice-1', expires_at: future };
  const h = harness({ deleting: true, routeChoice: { pending } });
  await h.handler(request(routeChoiceAction()), {});
  assert.equal(h.out.metadata.statusCode, 403);
  assert.equal(h.calls.some((c) => c.target === 'routeStart'), false);
});

test('B112: an unverified-age user cannot resume a paused run', async () => {
  const h = harness();
  await h.handler(request(ACTION, { ageVerified: 'false' }), {});
  assert.equal(h.out.metadata.statusCode, 403);
  assert.equal(h.body().error.code, 'onboarding_required');
  assert.equal(h.calls.length, 0);
});

test('B112: an unverified-age user cannot start a run through a route choice either', async () => {
  const future = Math.floor(Date.now() / 1000) + 300;
  const pending = { message: 'x', mode: 'science', surface_id: 'router-choice-1', expires_at: future };
  const h = harness({ routeChoice: { pending } });
  await h.handler(request(routeChoiceAction(), { ageVerified: 'false' }), {});
  assert.equal(h.out.metadata.statusCode, 403);
  assert.equal(h.calls.some((c) => c.target === 'routeStart'), false);
});

test('actionResponse echoes the action and attaches a status and message', () => {
  const response = actionResponse(routeChoiceAction(), 'completed');
  assert.equal(response.action_id, ROUTE_CHOICE_ACTION_ID);
  assert.equal(response.status, 'completed');
  assert.equal(response.message, null);
});
