'use strict';

const MAX_BODY_BYTES = 32 * 1024;
const MAX_EVENT_BYTES = 300 * 1024;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
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
  if (typeof input.message !== 'string' || !input.message.trim() || input.message.length > 16000 || input.task_type !== 'live-research' || !['science', 'business'].includes(input.mode)) throw new RequestError(400, 'invalid_request', 'The research request is invalid.');
  return { userId: sub, bearerToken: match[1], request: { session_id: input.session_id, run_id: input.run_id, message: input.message.trim(), task_type: input.task_type, mode: input.mode, ...(input.resume_from_seq !== undefined ? { resume_from_seq: input.resume_from_seq } : {}) } };
}

const partition = ({ userId, request }) => `sse#${userId}#${request.session_id}#${request.run_id}`;
const frame = (event) => `id: ${event.seq}\nevent: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`;

/** Parse SSE frames from an async iterable of bytes, including split UTF-8 and CRLF. */
async function* parseEvents(body) {
  const decoder = new TextDecoder(); let buffer = '';
  for await (const chunk of body) {
    buffer += decoder.decode(chunk, { stream: true });
    if (Buffer.byteLength(buffer) > MAX_EVENT_BYTES * 2) throw new Error('oversized frame');
    let match;
    while ((match = /\r?\n\r?\n/.exec(buffer))) {
      const raw = buffer.slice(0, match.index); buffer = buffer.slice(match.index + match[0].length);
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
  }
  if (buffer.trim()) throw new Error('incomplete frame');
}

/** Dependencies are injectable; production adapter below uses scoped AWS IAM. */
function createHandler(responseFrom, deps) {
  return async (event, rawStream, context = {}) => {
    let stream, parsed, started = false, reserved = false, closed = false, timer, stage = 'validate';
    const requestId = event?.requestContext?.requestId || context.awsRequestId || 'unknown';
    const log = (level, outcome, error) => console[level](JSON.stringify({ event: 'session_runner', request_id: requestId, lambda_request_id: context.awsRequestId, stage, outcome, ...(error ? { error_type: error.name || 'Error' } : {}) }));
    const send = (value) => { if (!closed) { try { stream.write(value); } catch (error) { closed = true; log('error', 'write_failed', error); } } };
    const respondError = (err) => {
      const e = err instanceof RequestError ? err : new RequestError(500, 'internal_error', 'The request could not be completed.');
      stream = responseFrom(rawStream, { statusCode: e.statusCode, headers: deps.headers(event, false) });
      stream.end(JSON.stringify({ error: { code: e.code, message: e.message, retriable: e.retriable, request_id: event?.requestContext?.requestId || '' } }));
    };
    try {
      parsed = validateRequest(event);
      stage = 'ownership';
      if (!await deps.owns(parsed.userId, parsed.request.session_id)) throw new RequestError(404, 'not_found', 'Session not found.');
      const key = partition(parsed);
      if (parsed.request.resume_from_seq !== undefined) {
        stage = 'replay';
        if (!await deps.exists(key)) throw new RequestError(404, 'not_found', 'Response not found.');
        stream = responseFrom(rawStream, { statusCode: 200, headers: deps.headers(event, true) });
        stream.on?.('error', () => { closed = true; });
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
        if (!closed) stream.end();
        return;
      }
      // A run id is reserved before the Router starts. A retry can replay but cannot start it twice.
      stage = 'reserve';
      await deps.reserve(key, parsed.userId, parsed.request);
      reserved = true;
      stage = 'router_start';
      const route = await deps.start(parsed.bearerToken, parsed.request, requestId);
      started = true;
      if (route.run_id !== parsed.request.run_id || route.session_id !== parsed.request.session_id || route.user_id !== parsed.userId || !route.gateway_token) throw new Error('invalid router response');
      stream = responseFrom(rawStream, { statusCode: 200, headers: deps.headers(event, true) });
      stream.on?.('error', () => { closed = true; });
      timer = setInterval(() => send(': heartbeat\n\n'), 17000);
      let seq = -1, terminal = false;
      try {
        stage = 'runtime_open';
        const body = await deps.open(route, parsed);
        stage = 'runtime_stream';
        for await (const item of parseEvents(body)) {
          if (item.seq !== seq + 1 || (seq === -1 && item.type !== 'surface_begin')) throw new Error('invalid event order');
          await deps.append(key, item); seq = item.seq; send(frame(item));
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
      log(err instanceof RequestError && err.statusCode < 500 ? 'warn' : 'error', 'request_failed', err);
      if (reserved && !stream && parsed) {
        try { await deps.append(partition(parsed), { seq: 0, type: 'error', code: 'service_unavailable', message: 'The research service is unavailable.', retriable: true }); } catch (error) { stage = 'persist_terminal_error'; log('error', 'persistence_failed', error); }
      }
      if (!stream) respondError(err);
      else if (!closed) stream.end();
    } finally {
      if (timer) clearInterval(timer);
      if (started && parsed) { try { await deps.finish(parsed.bearerToken, parsed.request); } catch (error) { stage = 'router_finish'; log('error', 'cleanup_failed', error); } }
    }
  };
}

function productionDependencies() {
  const { DynamoDBClient, GetItemCommand, PutItemCommand, QueryCommand, TransactWriteItemsCommand } = require('@aws-sdk/client-dynamodb');
  const { LambdaClient, InvokeCommand } = require('@aws-sdk/client-lambda');
  const db = new DynamoDBClient({}); const lambda = new LambdaClient({});
  const table = process.env.CEI_CHAT_HISTORY_TABLE, metadata = process.env.CEI_CHAT_METADATA_TABLE, runs = process.env.CEI_ACTIVE_RUNS_TABLE;
  const allowedOrigins = new Set((process.env.ALLOWED_ORIGINS || '').split(',').filter(Boolean));
  async function invoke(payload) {
    const result = await lambda.send(new InvokeCommand({ FunctionName: process.env.CEI_ROUTER_FUNCTION, Payload: Buffer.from(JSON.stringify(payload)) }));
    if (result.FunctionError) throw new RequestError(503, 'service_unavailable', 'The research service is unavailable.', true);
    let value; try { value = JSON.parse(Buffer.from(result.Payload || []).toString()); } catch { throw new RequestError(503, 'service_unavailable', 'The research service is unavailable.', true); }
    if (value.status !== 200) {
      const code = ['invalid_request', 'unauthorized', 'not_found', 'concurrency_limit'].includes(value.code) ? value.code : 'service_unavailable';
      throw new RequestError([400, 401, 404, 429].includes(value.status) ? value.status : 503, code, code === 'concurrency_limit' ? 'Too many active runs.' : 'The research service is unavailable.', code === 'service_unavailable');
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
    async exists(key) { const r = await db.send(new GetItemCommand({ TableName: table, Key: { session_id: { S: key }, sequence_number: { N: '-1' } }, ConsistentRead: true })); return !!r.Item; },
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
      const response = await fetch(url, { method: 'POST', redirect: 'error', headers: { Authorization: `Bearer ${parsed.bearerToken}`, 'X-Cei-Gateway-Token': route.gateway_token, 'X-Amzn-Bedrock-AgentCore-Runtime-Session-Id': parsed.request.session_id, Accept: 'text/event-stream', 'Content-Type': 'application/json' }, body: JSON.stringify({ prompt: parsed.request.message, mode: parsed.request.mode, stream: true }), signal: AbortSignal.timeout(850000) });
      if (!response.ok || !response.body || !response.headers.get('content-type')?.includes('text/event-stream')) throw new Error('runtime unavailable');
      return response.body;
    },
  };
}

if (globalThis.awslambda) exports.handler = globalThis.awslambda.streamifyResponse(createHandler((stream, metadata) => globalThis.awslambda.HttpResponseStream.from(stream, metadata), productionDependencies()));
exports.createHandler = createHandler;
exports.validateRequest = validateRequest;
exports.parseEvents = parseEvents;
exports.partition = partition;
