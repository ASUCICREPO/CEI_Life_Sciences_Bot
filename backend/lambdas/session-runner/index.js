'use strict';

const MAX_BODY_BYTES = 32 * 1024;
const MAX_EVENT_BYTES = 300 * 1024;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
// F9/B15/live-drafting: the two deterministic dashboard tasks (both are streaming agents, see STREAMING_AGENTS).
const DASHBOARD_TASK_TYPES = new Set(['live-research', 'live-drafting']);
const TYPES = new Set(['surface_begin', 'component_add', 'text_delta', 'component_update', 'grounding_update', 'trace_event', 'progress', 'usage', 'surface_end', 'error', 'hitl_request', 'job_handoff']);
const TERMINAL = new Set(['surface_end', 'error', 'hitl_request', 'job_handoff']);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

class RequestError extends Error {
  constructor(statusCode, code, message, retriable = false) {
    super(message); Object.assign(this, { statusCode, code, retriable });
  }
}

function validateRequest(event) {
  const sub = event?.requestContext?.authorizer?.claims?.sub;
  if (typeof sub !== 'string' || !sub.trim()) throw new RequestError(401, 'unauthorized', 'Sign in again to continue.');
  const headers = event.headers && typeof event.headers === 'object' ? event.headers : {};
  const authorization = Object.entries(headers).find(([key]) => key.toLowerCase() === 'authorization')?.[1];
  const match = typeof authorization === 'string' ? /^Bearer\s+(\S+)$/i.exec(authorization) : null;
  if (!match) throw new RequestError(401, 'unauthorized', 'Sign in again to continue.');
  if (typeof event.body !== 'string') throw new RequestError(400, 'invalid_request', 'A JSON request is required.');
  let bytes;
  if (event.isBase64Encoded) {
    if (!/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(event.body)) throw new RequestError(400, 'invalid_request', 'The request body is invalid.');
    bytes = Buffer.from(event.body, 'base64');
  } else bytes = Buffer.from(event.body, 'utf8');
  if (bytes.length > MAX_BODY_BYTES) throw new RequestError(400, 'invalid_request', 'The request is too large.');
  let input;
  try { input = JSON.parse(bytes.toString('utf8')); } catch { throw new RequestError(400, 'invalid_request', 'The request body is invalid.'); }
  if (!input || typeof input !== 'object' || Array.isArray(input)) throw new RequestError(400, 'invalid_request', 'The request body is invalid.');
  if (!UUID.test(input.session_id || '') || !UUID.test(input.run_id || '')) throw new RequestError(400, 'invalid_request', 'A valid session and run are required.');
  if (input.resume_from_seq !== undefined && (!Number.isSafeInteger(input.resume_from_seq) || input.resume_from_seq < 0)) throw new RequestError(400, 'invalid_request', 'The replay position is invalid.');
  // F9/B15: "live-research" and "live-drafting" are today's deterministic dashboard tasks (unchanged); an
  // absent task_type is free text, routed by the Session Router's classifier. Any other value is invalid.
  const taskType = input.task_type;
  if (typeof input.message !== 'string' || !input.message.trim() || input.message.length > 16000
      || (taskType !== undefined && !DASHBOARD_TASK_TYPES.has(taskType)) || !['science', 'business'].includes(input.mode)) {
    throw new RequestError(400, 'invalid_request', 'The chat request is invalid.');
  }
  return { userId: sub, bearerToken: match[1], request: { session_id: input.session_id, run_id: input.run_id, message: input.message.trim(), task_type: taskType, mode: input.mode, ...(input.resume_from_seq !== undefined ? { resume_from_seq: input.resume_from_seq } : {}) } };
}

const partition = ({ userId, request }) => `sse#${userId}#${request.session_id}#${request.run_id}`;
const frame = (event) => `id: ${event.seq}\nevent: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`;

// F9: CEI UI 1.1 surface envelope constants and the fixed ChoicePicker/route-choice contract. ui-actions's
// index.js must agree on ROUTE_CHOICE_* (it validates a submitted action against a Router-persisted record
// keyed by these same ids); session-runner is the only place that renders the ChoicePicker surface itself.
const CEI_UI_CONTENT_TYPE = 'application/vnd.cei.ui+json';
const CEI_UI_PROTOCOL = 'cei-ui';
const SURFACE_BEGIN_VERSION = '1.2'; // the SSE transport version (research.py's ResearchEventStream.begin());
                                      // the surface's own embedded "version" field (CEI UI 1.1) is separate.
const ROUTE_CHOICE_INTERACTION_ID = 'router-choice';
const ROUTE_CHOICE_COMPONENT_ID = 'router-choice';
const ROUTE_CHOICE_ACTION_ID = 'route-choice';
const ROUTE_CHOICE_OPTIONS = [
  { label: 'Research', value: 'research', description: 'Deep research across data sources with citations.' },
  { label: 'Drafting', value: 'drafting', description: 'Prepare a document (grant, pitch, paper, whitepaper).' },
  { label: 'Analysis', value: 'analysis', description: 'Competitor, market or legal landscape analysis.' },
  { label: 'Pipeline Manager', value: 'pipeline_manager', description: 'Multi-step work across specialists.' },
];

function choicePickerSurface(surfaceId) {
  return {
    content_type: CEI_UI_CONTENT_TYPE, protocol: CEI_UI_PROTOCOL, version: '1.1',
    surface_id: surfaceId, revision: 1, root_id: ROUTE_CHOICE_COMPONENT_ID,
    components: [{
      id: ROUTE_CHOICE_COMPONENT_ID, type: 'ChoicePicker', interaction_id: ROUTE_CHOICE_INTERACTION_ID,
      label: 'Which kind of help do you want?', multi: false, options: ROUTE_CHOICE_OPTIONS,
      action: { action_id: ROUTE_CHOICE_ACTION_ID, label: 'Choose', intent: 'select' },
    }],
  };
}

/** Parent-first walk of a CEI UI surface's components, mirroring research.py's ResearchEventStream.components(). */
function* walkComponents(ui, rootId) {
  const byId = new Map((ui.components || []).map((component) => [component.id, component]));
  const seen = new Set();
  function* visit(id, parentId, index) {
    if (seen.has(id) || !byId.has(id)) throw new Error('invalid surface component graph');
    seen.add(id);
    const component = { ...byId.get(id) };
    let children;
    if (component.type === 'Tabs') children = (component.tabs || []).map((tab) => tab.child_id);
    else if (Array.isArray(component.children)) { children = component.children; component.children = []; }
    else children = [];
    const record = { parent_id: parentId, component };
    if (parentId !== null) record.index = index;
    yield record;
    let position = 0;
    for (const childId of children) { yield* visit(childId, id, position); position += 1; }
  }
  yield* visit(rootId, null, 0);
}

const surfaceBeginEvent = (ui) => ({ seq: 0, type: 'surface_begin', surface_id: ui.surface_id, revision: ui.revision || 1,
  content_type: CEI_UI_CONTENT_TYPE, protocol: CEI_UI_PROTOCOL, version: SURFACE_BEGIN_VERSION, root_id: ui.root_id });

/** One buffered `op: run` JSON envelope (a non-Research specialist) -> the same SSE contract every agent uses. */
function bufferedToEvents(envelope) {
  const status = envelope && (envelope.status || (typeof envelope.text === 'string' ? 'completed' : undefined));
  if (status === 'awaiting_input' && envelope.ui) {
    // Same shape as a Research clarify pause: surface_begin, then one hitl_request carrying the full
    // pending surface (research/nodes.py's extract_interrupt_surface via awaiting_envelope()).
    return [surfaceBeginEvent(envelope.ui), { seq: 1, type: 'hitl_request', ...envelope.ui }];
  }
  if (status === 'completed') {
    const ui = envelope.ui || {
      content_type: CEI_UI_CONTENT_TYPE, protocol: CEI_UI_PROTOCOL, version: '1.1', surface_id: 'agent-final',
      revision: 1, root_id: 'root', components: [{ id: 'root', type: 'Text', content: envelope.text || 'No response generated.' }],
    };
    const events = [surfaceBeginEvent(ui)];
    let seq = 1;
    for (const record of walkComponents(ui, ui.root_id)) { events.push({ seq, type: 'component_add', ...record }); seq += 1; }
    events.push({ seq, type: 'surface_end', surface_id: ui.surface_id, revision: ui.revision || 1, root_id: ui.root_id, final: true });
    return events;
  }
  return [
    { seq: 0, type: 'surface_begin', surface_id: 'agent-error', revision: 1, content_type: CEI_UI_CONTENT_TYPE, protocol: CEI_UI_PROTOCOL, version: SURFACE_BEGIN_VERSION, root_id: 'root' },
    { seq: 1, type: 'error', code: 'service_unavailable', message: 'The response could not be completed.', retriable: true },
  ];
}

/** B80: the Router's ChoicePicker turn (no agent run); surface_begin then one terminal hitl_request. */
function choiceRequiredEvents(surfaceId) {
  const surface = choicePickerSurface(surfaceId);
  return [surfaceBeginEvent(surface), { seq: 1, type: 'hitl_request', ...surface }];
}

async function* fromArray(items) { for (const item of items) yield item; }

// ADR: Drafting now streams like Research (op: run, body {prompt, mode, stream:true}) | Rationale: the
// Drafting runtime (drafting_main.py) renders its own live surface (traces/components/HITL outline) the
// same way Research does; Analysis and Pipeline Manager remain single buffered envelopes (requestBodyFor).
const STREAMING_AGENTS = new Set(['research', 'drafting']);

/** Per-agent request body for the AgentCore `op: run` default; streaming agents are never built here. */
function requestBodyFor(agent, parsed) {
  // ADR: free-text drafting carries no document_type | drafting_main.py now defaults to "whitepaper" itself.
  return { prompt: parsed.request.message, mode: parsed.request.mode };
}

/** Parse SSE frames from an async iterable of bytes, including split UTF-8 and CRLF. */
async function* parseEvents(body) {
  const decoder = new TextDecoder('utf-8', { fatal: true }); let buffer = '';
  for await (const chunk of body) {
    buffer += decoder.decode(chunk, { stream: true });
    let match;
    while ((match = /\r?\n\r?\n/.exec(buffer))) {
      const raw = buffer.slice(0, match.index); buffer = buffer.slice(match.index + match[0].length);
      if (Buffer.byteLength(raw) > MAX_EVENT_BYTES * 2) throw new Error('oversized frame');
      if (raw.startsWith(':')) continue;
      const lines = raw.split(/\r?\n/);
      const id = lines.find((line) => line.startsWith('id:'))?.slice(3).trim();
      const type = lines.find((line) => line.startsWith('event:'))?.slice(6).trim();
      const data = lines.filter((line) => line.startsWith('data:')).map((line) => line.slice(5).trimStart()).join('\n');
      if (!/^\d+$/.test(id || '') || !TYPES.has(type) || !data || Buffer.byteLength(data) > MAX_EVENT_BYTES) throw new Error('invalid frame');
      const payload = JSON.parse(data); const seq = Number(id);
      if (!Number.isSafeInteger(seq) || !payload || typeof payload !== 'object' || Array.isArray(payload) || (payload.type && payload.type !== type)) throw new Error('invalid event');
      yield { ...payload, type, seq };
    }
    // One transport chunk can contain many valid events; bound the unfinished
    // frame, not the total size of the chunk received from the runtime.
    if (Buffer.byteLength(buffer) > MAX_EVENT_BYTES * 2) throw new Error('oversized frame');
  }
  buffer += decoder.decode();
  if (buffer.trim()) throw new Error('incomplete frame');
}

/** Dependencies are injectable; production adapter below uses scoped AWS IAM. */
function createHandler(responseFrom, deps) {
  return async (event, rawStream, context = {}) => {
    let stream, parsed, started = false, reserved = false, closed = false, timer, stage = 'validate';
    const requestId = event?.requestContext?.requestId || context.awsRequestId || 'unknown';
    const log = (level, outcome, error) => console[level](JSON.stringify({ event: 'session_runner', request_id: requestId, lambda_request_id: context.awsRequestId, stage, outcome, ...(error ? { error_type: error.name || 'Error' } : {}), ...(error instanceof RequestError ? { code: error.code, status: error.statusCode } : {}) }));
    const send = (value) => { if (!closed) { try { stream.write(value); } catch (error) { closed = true; log('error', 'write_failed', error); } } };
    const respondError = (err) => {
      const e = err instanceof RequestError ? err : new RequestError(500, 'internal_error', 'The request could not be completed.');
      stream = responseFrom(rawStream, { statusCode: e.statusCode, headers: deps.headers(event, false) });
      // Lambda writes the HTTP metadata and delimiter on the first write(), not end(body).
      stream.write(JSON.stringify({ error: { code: e.code, message: e.message, retriable: e.retriable, request_id: event?.requestContext?.requestId || '' } }));
      stream.end();
    };
    try {
      parsed = validateRequest(event);
      stage = 'ownership';
      if (!await deps.owns(parsed.userId, parsed.request.session_id)) throw new RequestError(404, 'not_found', 'Session not found.');
      const key = partition(parsed);
      if (parsed.request.resume_from_seq !== undefined) {
        stage = 'replay';
        if (!await deps.exists(key)) throw new RequestError(404, 'not_found', 'Response not found.');
        // A resume cursor represents an event the client has already received.
        // Validate it against durable storage before opening the SSE response.
        const cursor = await deps.at(key, parsed.request.resume_from_seq);
        if (!cursor || cursor.seq !== parsed.request.resume_from_seq) throw new RequestError(409, 'invalid_replay_position', 'The replay position is unavailable.');
        stream = responseFrom(rawStream, { statusCode: 200, headers: deps.headers(event, true) });
        stream.on?.('error', (error) => { closed = true; log('error', 'stream_transport_failed', error); });
        send(': connected\n\n');
        if (TERMINAL.has(cursor.type)) { stream.end(); return; }
        let seq = parsed.request.resume_from_seq, lastHeartbeat = Date.now();
        while (!closed && (context.getRemainingTimeInMillis?.() ?? 900000) > 5000) {
          const events = await deps.after(key, seq);
          for (const item of events) {
            if (item.seq !== seq + 1) throw new Error('replay sequence gap');
            send(frame(item)); seq = item.seq;
            if (TERMINAL.has(item.type)) { stream.end(); return; }
          }
          if (Date.now() - lastHeartbeat >= 17000) { send(': heartbeat\n\n'); lastHeartbeat = Date.now(); }
          await deps.sleep(500);
        }
        if (!closed) { log('error', 'replay_timeout'); stream.end(); }
        return;
      }
      // B109/F19: a replay (above) reads data that already exists; only a new run is gated here.
      stage = 'account_gate';
      if (await deps.accountDeleting(parsed.userId)) throw new RequestError(403, 'forbidden', 'This account is being deleted.');
      // A run id is reserved before the Router starts. A retry can replay but cannot start it twice.
      stage = 'reserve';
      await deps.reserve(key, parsed.userId, parsed.request);
      reserved = true;
      stage = 'router_start';
      const route = await deps.start(parsed.bearerToken, parsed.request, requestId);
      started = true;
      if (route.run_id !== parsed.request.run_id || route.session_id !== parsed.request.session_id || route.user_id !== parsed.userId) throw new Error('invalid router response');
      // F9/B80: a ChoicePicker turn never runs an agent, so it never carries a gateway token.
      if (!route.choice_required && !route.gateway_token) throw new Error('invalid router response');
      stream = responseFrom(rawStream, { statusCode: 200, headers: deps.headers(event, true) });
      stream.on?.('error', (error) => { closed = true; log('error', 'stream_transport_failed', error); });
      send(': connected\n\n');
      timer = setInterval(() => send(': heartbeat\n\n'), 17000);
      let seq = -1, terminal = false;
      try {
        stage = 'runtime_open';
        // F9/live-drafting: a ChoicePicker turn is a fixed two-event stream; Research and Drafting always
        // stream live; every other agent returns one buffered JSON envelope, adapted into the same SSE
        // contract (bufferedToEvents). Streaming is keyed on the routed agent, not task_type: a free-text
        // route to drafting streams the same way the deterministic live-drafting task does.
        const isStreaming = !route.choice_required && (!route.agent || STREAMING_AGENTS.has(route.agent));
        const source = route.choice_required
          ? fromArray(choiceRequiredEvents(route.surface_id))
          : isStreaming
            ? parseEvents(await deps.open(route, parsed))
            : fromArray(bufferedToEvents(await deps.invokeBuffered(route, parsed)));
        stage = 'runtime_stream';
        for await (const item of source) {
          if (item.seq !== seq + 1 || (seq === -1 && item.type !== 'surface_begin')) throw new Error('invalid event order');
          stage = 'persist_event'; await deps.append(key, item); seq = item.seq;
          stage = 'client_stream'; send(frame(item));
          stage = 'runtime_stream';
          if (item.type === 'error') log('error', 'agent_error');
          if (TERMINAL.has(item.type)) { terminal = true; break; }
        }
        if (!terminal) throw new Error('missing terminal event');
      } catch (error) {
        log('error', 'stream_failed', error);
        stage = 'persist_terminal_error';
        const item = { seq: seq + 1, type: 'error', code: 'stream_failed', message: 'The response could not be completed.', retriable: true };
        await deps.append(key, item); send(frame(item));
      }
      if (!closed) stream.end();
    } catch (err) {
      const clientError = err instanceof RequestError && err.statusCode < 500;
      log(clientError ? 'warn' : 'error', clientError ? 'client_rejected' : 'request_failed', err);
      if (reserved && !stream && parsed) {
        const terminalError = clientError
          ? { seq: 0, type: 'error', code: err.code, message: err.message, retriable: err.retriable }
          : { seq: 0, type: 'error', code: 'service_unavailable', message: 'The research service is unavailable.', retriable: true };
        try { await deps.append(partition(parsed), terminalError); } catch (error) { stage = 'persist_terminal_error'; log('error', 'persistence_failed', error); }
      }
      if (!stream) respondError(err);
      else if (!closed) stream.end();
    } finally {
      if (timer) clearInterval(timer);
      if (started && parsed) { try { await deps.finish(parsed.bearerToken, parsed.request); } catch (error) { stage = 'router_finish'; log('error', 'cleanup_failed', error); } }
    }
  };
}

function routerError(value) {
  const codes = ['invalid_request', 'unauthorized', 'not_found', 'concurrency_limit', 'model_unavailable'];
  const code = codes.includes(value?.code) ? value.code : 'service_unavailable';
  const messages = { concurrency_limit: 'Too many active runs.', model_unavailable: "This chat's model is unavailable." };
  return new RequestError([400, 401, 404, 429].includes(value?.status) ? value.status : 503,
    code, messages[code] || 'The research service is unavailable.', code === 'service_unavailable');
}

function productionDependencies() {
  const { DynamoDBClient, GetItemCommand, PutItemCommand, QueryCommand, TransactWriteItemsCommand } = require('@aws-sdk/client-dynamodb');
  const { LambdaClient, InvokeCommand } = require('@aws-sdk/client-lambda');
  const { isAccountDeleting } = require('../shared/account-gate');
  const db = new DynamoDBClient({}); const lambda = new LambdaClient({});
  const table = process.env.CEI_CHAT_HISTORY_TABLE, metadata = process.env.CEI_CHAT_METADATA_TABLE, runs = process.env.CEI_ACTIVE_RUNS_TABLE;
  const userMetadata = process.env.CEI_USER_METADATA_TABLE;
  const allowedOrigins = new Set((process.env.ALLOWED_ORIGINS || '').split(',').filter(Boolean));
  async function invoke(payload) {
    const result = await lambda.send(new InvokeCommand({ FunctionName: process.env.CEI_ROUTER_FUNCTION, Payload: Buffer.from(JSON.stringify(payload)) }));
    if (result.FunctionError) throw new RequestError(503, 'service_unavailable', 'The research service is unavailable.', true);
    let value; try { value = JSON.parse(Buffer.from(result.Payload || []).toString()); } catch { throw new RequestError(503, 'service_unavailable', 'The research service is unavailable.', true); }
    if (value.status !== 200) {
      throw routerError(value);
    }
    return value;
  }
  return {
    sleep,
    headers(event, sse) {
      const origin = Object.entries(event.headers || {}).find(([k]) => k.toLowerCase() === 'origin')?.[1];
      return { 'Content-Type': sse ? 'text/event-stream' : 'application/json', 'Cache-Control': 'no-cache, no-transform', ...(allowedOrigins.has(origin) ? { 'Access-Control-Allow-Origin': origin, Vary: 'Origin' } : {}) };
    },
    async owns(userId, sessionId) { const r = await db.send(new GetItemCommand({ TableName: metadata, Key: { user_id: { S: userId }, session_id: { S: sessionId } }, ConsistentRead: true })); return !!r.Item; },
    async accountDeleting(userId) { return isAccountDeleting(db, userMetadata, userId, GetItemCommand); },
    async exists(key) { const r = await db.send(new GetItemCommand({ TableName: table, Key: { session_id: { S: key }, sequence_number: { N: '-1' } }, ConsistentRead: true })); return !!r.Item; },
    async at(key, seq) {
      const r = await db.send(new GetItemCommand({ TableName: table, Key: { session_id: { S: key }, sequence_number: { N: String(seq) } }, ConsistentRead: true }));
      return r.Item?.event?.S ? JSON.parse(r.Item.event.S) : null;
    },
    async reserve(key, userId, request) {
      // The index makes the synthetic history partition discoverable during the F19 deletion cascade.
      // Reserving both rows atomically prevents orphaned event partitions if either write fails.
      try { await db.send(new TransactWriteItemsCommand({ TransactItems: [
        { Put: { TableName: table, Item: { session_id: { S: key }, sequence_number: { N: '-1' }, user_id: { S: userId }, record_type: { S: 'sse_head' } }, ConditionExpression: 'attribute_not_exists(session_id)' } },
        { Put: { TableName: runs, Item: { user_id: { S: userId }, record: { S: `replay#${request.session_id}#${request.run_id}` }, history_partition: { S: key } }, ConditionExpression: 'attribute_not_exists(user_id)' } },
      ] })); }
      catch (e) { if (e.name === 'TransactionCanceledException' && e.CancellationReasons?.some((r) => r.Code === 'ConditionalCheckFailed')) throw new RequestError(409, 'run_exists', 'This response already started. Reconnect to continue.'); throw e; }
    },
    async append(key, item) { await db.send(new PutItemCommand({ TableName: table, Item: { session_id: { S: key }, sequence_number: { N: String(item.seq) }, record_type: { S: 'sse_event' }, event: { S: JSON.stringify(item) } }, ConditionExpression: 'attribute_not_exists(session_id)' })); },
    async after(key, seq) {
      const r = await db.send(new QueryCommand({ TableName: table, KeyConditionExpression: 'session_id = :pk AND sequence_number > :seq', ExpressionAttributeValues: { ':pk': { S: key }, ':seq': { N: String(seq) } }, ConsistentRead: true, Limit: 100 }));
      return (r.Items || []).map((i) => JSON.parse(i.event.S));
    },
    async start(bearer, request, requestId) { return invoke({ op: 'start', bearer_token: bearer, request, request_id: requestId }); },
    async finish(bearer, request) { await invoke({ op: 'finish', bearer_token: bearer, session_id: request.session_id, run_id: request.run_id }); },
    async open(route, parsed) {
      const url = new URL(route.runtime_url);
      if (url.protocol !== 'https:' || !/^bedrock-agentcore\.[a-z0-9-]+\.amazonaws\.com(?:\.cn)?$/.test(url.hostname) || url.port || url.username || url.password || !/^\/runtimes\/[^/]+\/invocations$/.test(url.pathname) || url.search !== '?qualifier=DEFAULT') throw new Error('invalid runtime URL');
      // ADR: X-Cei-Gateway-Token only when the route provides one | Rationale: only Research's runtime
      // allowlists this header (agent-runtimes.ts configureResearchGateway); Drafting now streams through
      // this same fetch but has no Gateway wiring, so the header must be omitted rather than sent empty.
      const response = await fetch(url, { method: 'POST', redirect: 'error', headers: { Authorization: `Bearer ${parsed.bearerToken}`, ...(route.gateway_token && route.agent !== 'drafting' ? { 'X-Cei-Gateway-Token': route.gateway_token } : {}), 'X-Amzn-Bedrock-AgentCore-Runtime-Session-Id': parsed.request.session_id, ...(process.env._X_AMZN_TRACE_ID ? { 'X-Amzn-Trace-Id': process.env._X_AMZN_TRACE_ID } : {}), Accept: 'text/event-stream', 'Content-Type': 'application/json' }, body: JSON.stringify({ prompt: parsed.request.message, mode: parsed.request.mode, stream: true }), signal: AbortSignal.timeout(850000) });
      if (!response.ok || !response.body || !response.headers.get('content-type')?.includes('text/event-stream')) throw new Error('runtime unavailable');
      return response.body;
    },
    // F9/live-drafting: Analysis/Pipeline Manager's `op: run` (default) is always a single buffered JSON
    // envelope, never SSE (Research and Drafting stream instead, via open() above). Same URL-shape guard.
    async invokeBuffered(route, parsed) {
      const url = new URL(route.runtime_url);
      if (url.protocol !== 'https:' || !/^bedrock-agentcore\.[a-z0-9-]+\.amazonaws\.com(?:\.cn)?$/.test(url.hostname) || url.port || url.username || url.password || !/^\/runtimes\/[^/]+\/invocations$/.test(url.pathname) || url.search !== '?qualifier=DEFAULT') throw new Error('invalid runtime URL');
      const response = await fetch(url, { method: 'POST', redirect: 'error', headers: { Authorization: `Bearer ${parsed.bearerToken}`, 'X-Amzn-Bedrock-AgentCore-Runtime-Session-Id': parsed.request.session_id, ...(process.env._X_AMZN_TRACE_ID ? { 'X-Amzn-Trace-Id': process.env._X_AMZN_TRACE_ID } : {}), Accept: 'application/json', 'Content-Type': 'application/json' }, body: JSON.stringify(requestBodyFor(route.agent, parsed)), signal: AbortSignal.timeout(850000) });
      if (!response.ok) throw new Error('runtime unavailable');
      const envelope = await response.json();
      if (!envelope || typeof envelope !== 'object' || Array.isArray(envelope)) throw new Error('invalid runtime response');
      return envelope;
    },
  };
}

if (globalThis.awslambda) exports.handler = globalThis.awslambda.streamifyResponse(createHandler((stream, metadata) => globalThis.awslambda.HttpResponseStream.from(stream, metadata), productionDependencies()));
exports.createHandler = createHandler;
exports.validateRequest = validateRequest;
exports.parseEvents = parseEvents;
exports.partition = partition;
exports.routerError = routerError;
exports.choicePickerSurface = choicePickerSurface;
exports.bufferedToEvents = bufferedToEvents;
exports.choiceRequiredEvents = choiceRequiredEvents;
exports.requestBodyFor = requestBodyFor;
exports.walkComponents = walkComponents;
exports.STREAMING_AGENTS = STREAMING_AGENTS;
exports.DASHBOARD_TASK_TYPES = DASHBOARD_TASK_TYPES;
