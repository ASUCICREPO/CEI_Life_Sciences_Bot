'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { createHandler, validateRequest, parseEvents } = require('./index');
const session = '6f1c2d3e-0000-4000-8000-000000000001';
const run = '6f1c2d3e-0000-4000-8000-000000000002';
function request(extra = {}) { return { requestContext: { requestId: 'request-1', authorizer: { claims: { sub: 'user-1' } } }, headers: { Authorization: 'Bearer token' }, body: JSON.stringify({ session_id: session, run_id: run, message: 'question', task_type: 'live-research', mode: 'science', ...extra }) }; }
function harness() {
  const events = [], heads = new Set(), calls = [], streamEvents = {};
  const deps = {
    headers: (_event, sse) => ({ 'Content-Type': sse ? 'text/event-stream' : 'application/json' }),
    owns: async (user, id) => user === 'user-1' && id === session,
    exists: async (key) => heads.has(key),
    reserve: async (key) => { if (heads.has(key)) throw new Error('duplicate'); heads.add(key); },
    append: async (_key, item) => { events.push(item); },
    after: async (_key, seq) => events.filter((e) => e.seq > seq),
    start: async () => { calls.push('start'); return { user_id: 'user-1', session_id: session, run_id: run, gateway_token: 'secret' }; },
    finish: async () => calls.push('finish'),
    open: async () => (async function* () { yield Buffer.from('id: 0\nevent: surface_begin\ndata: {"root_id":"root"}\n\nid: 1\nevent: surface_end\ndata: {"root_id":"root"}\n\n'); })(),
    sleep: async () => {},
  };
  async function invoke(event) {
    let metadata, text = '';
    const handler = createHandler((_raw, m) => { metadata = m; return { on(name, callback) { streamEvents[name] = callback; }, write(value) { text += value; }, end(value = '') { text += value; } }; }, deps);
    await handler(event, {}, { getRemainingTimeInMillis: () => 60000 });
    return { metadata, text };
  }
  return { deps, events, calls, streamEvents, invoke };
}
test('uses authorizer identity and ignores user-supplied identity', () => {
  const parsed = validateRequest(request({ user_id: 'attacker' }));
  assert.equal(parsed.userId, 'user-1'); assert.equal(parsed.request.user_id, undefined);
});
test('rejects missing auth, invalid run ids and malformed payloads', async () => {
  const h = harness();
  for (const event of [{ ...request(), requestContext: {} }, request({ run_id: 'bad' }), { ...request(), body: '{' }]) {
    const result = await h.invoke(event); assert.ok([400, 401].includes(result.metadata.statusCode));
  }
  assert.deepEqual(h.calls, []);
});
test('writes early errors before ending the response', async () => {
  const calls = [];
  const handler = createHandler((_raw, metadata) => ({
    write(value) { calls.push(['write', value]); },
    end(value) { calls.push(['end', value]); },
  }), { headers: () => ({}) });
  await handler({ ...request(), requestContext: {} }, {}, {});
  assert.equal(calls[0][0], 'write');
  assert.equal(JSON.parse(calls[0][1]).error.code, 'unauthorized');
  assert.deepEqual(calls[1], ['end', undefined]);
});
test('router failures return framed JSON before any SSE starts', async () => {
  const h = harness(); h.deps.start = async () => { throw new Error('upstream details'); };
  const response = await h.invoke(request());
  assert.equal(response.metadata.statusCode, 500);
  assert.equal(JSON.parse(response.text).error.request_id, 'request-1');
  assert.doesNotMatch(response.text, /upstream details/);
});
test('persists before streaming and finishes the lease; replay does not start a second run', async () => {
  const h = harness(); const first = await h.invoke(request());
  assert.equal(first.metadata.statusCode, 200); assert.match(first.text, /event: surface_end/);
  assert.deepEqual(h.events.map((e) => e.seq), [0, 1]); assert.deepEqual(h.calls, ['start', 'finish']);
  const replay = await h.invoke(request({ resume_from_seq: 0 }));
  assert.doesNotMatch(replay.text, /surface_begin/); assert.match(replay.text, /surface_end/);
  assert.deepEqual(h.calls, ['start', 'finish']);
});
test('foreign sessions and missing replay heads never start a run', async () => {
  const h = harness(); h.deps.owns = async () => false;
  assert.equal((await h.invoke(request())).metadata.statusCode, 404);
  h.deps.owns = async () => true;
  assert.equal((await h.invoke(request({ resume_from_seq: 0 }))).metadata.statusCode, 404);
  assert.deepEqual(h.calls, []);
});
test('runtime failures yield a safe persisted terminal error and release the run', async () => {
  const h = harness(); h.deps.open = async () => { throw new Error('secret runtime failure'); };
  const result = await h.invoke(request()); assert.match(result.text, /event: error/);
  assert.doesNotMatch(result.text, /secret/); assert.equal(h.events[0].type, 'error'); assert.deepEqual(h.calls, ['start', 'finish']);
});
test('logs the failed stage without leaking error messages or bearer tokens', async () => {
  const h = harness(); h.deps.start = async () => { throw new Error('secret token and message'); };
  const messages = []; const original = console.error;
  console.error = (message) => messages.push(message);
  try { await h.invoke(request()); } finally { console.error = original; }
  const entry = JSON.parse(messages[0]);
  assert.equal(entry.stage, 'router_start'); assert.equal(entry.request_id, 'request-1');
  assert.doesNotMatch(messages.join(' '), /secret|token and message/);
});
test('logs an error event returned normally by the agent', async () => {
  const h = harness();
  h.deps.open = async () => (async function* () { yield Buffer.from('id: 0\nevent: surface_begin\ndata: {}\n\nid: 1\nevent: error\ndata: {"message":"sensitive"}\n\n'); })();
  const messages = []; const original = console.error;
  console.error = (message) => messages.push(message);
  try { await h.invoke(request()); } finally { console.error = original; }
  assert.equal(JSON.parse(messages[0]).outcome, 'agent_error');
  assert.doesNotMatch(messages.join(' '), /sensitive/);
});
test('identifies persistence failures without exposing details', async () => {
  const h = harness(); h.deps.append = async () => { throw new Error('private table data'); };
  const messages = []; const original = console.error;
  console.error = (message) => messages.push(message);
  let response;
  try { response = await h.invoke(request()); } finally { console.error = original; }
  assert.match(response.text, /^: connected\n\n/);
  assert.equal(JSON.parse(messages[0]).stage, 'persist_event');
  assert.doesNotMatch(messages.join(' '), /private table data/);
});
test('records asynchronous stream transport errors', async () => {
  const h = harness();
  h.deps.open = async () => { h.streamEvents.error(new Error('private transport data')); throw new Error('closed'); };
  const messages = []; const original = console.error;
  console.error = (message) => messages.push(message);
  try { await h.invoke(request()); } finally { console.error = original; }
  assert.ok(messages.some((line) => JSON.parse(line).outcome === 'stream_transport_failed'));
  assert.doesNotMatch(messages.join(' '), /private transport data/);
});
test('parser handles split CRLF, UTF-8 and multiple events', async () => {
  const bytes = Buffer.from('id: 0\r\nevent: text_delta\r\ndata: {"delta":"é"}\r\n\r\n');
  const out = []; for await (const event of parseEvents((async function* () { for (const byte of bytes) yield Buffer.from([byte]); })())) out.push(event);
  assert.equal(out[0].delta, 'é'); assert.equal(out[0].seq, 0);
});
