'use strict';

/**
 * POST /ui-actions (F13, api.md): validate a typed CEI UI ActionRequest and resume the paused run.
 *
 * - The caller identity is the Cognito authorizer `sub`; the session must belong to it (ChatMetadataStore).
 * - The owning agent comes from the session's stored agent_type, never from the request.
 * - The runtime is called with the caller's own bearer token (runtimes accept only SPA user tokens, B102), so
 *   AgentCore's authorizer and identity.py re-verify it. The runtime remains authoritative for the pydantic
 *   ActionRequest bounds, stale and duplicate actions (`stale`) and expired checkpoints (`expired`, B82).
 * - A resumed analysis can run for minutes, longer than a buffered API Gateway integration allows, so the route
 *   uses STREAM mode (as /chat/stream does) and this handler writes one JSON body.
 * Tokens, session ids and bodies are never logged.
 */

const MAX_BODY_BYTES = 32 * 1024;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const ACTION_FIELDS = ['request_id', 'session_id', 'surface_id', 'revision', 'interaction_id', 'component_id', 'action_id', 'value'];
const BOUNDED_STRINGS = { request_id: 128, surface_id: 128, interaction_id: 128, component_id: 128, action_id: 128 };
// agent_type stored by begin_turn -> AGENT_URLS key. Only agents whose graphs pause with interrupt() resume.
// F13: Research (clarify) and Drafting (outline approval) now pause too; resumes are still a single
// buffered JSON envelope (no SSE), delivered over this route's existing STREAM-mode Lambda response.
const RESUMABLE = { analysis: 'analysis', pipeline_manager: 'pipeline', research: 'research', drafting: 'drafting' };
const ENVELOPE_STATUSES = new Set(['completed', 'awaiting_input', 'running', 'rejected', 'stale', 'error']);
const RUNTIME_TIMEOUT_MS = 13.5 * 60 * 1000;

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
  if (!match || match[1].length > 8192) throw new RequestError(401, 'unauthorized', 'Sign in again to continue.');
  if (typeof event.body !== 'string') throw new RequestError(400, 'invalid_request', 'A JSON request is required.');
  const bytes = event.isBase64Encoded ? Buffer.from(event.body, 'base64') : Buffer.from(event.body, 'utf8');
  if (bytes.length > MAX_BODY_BYTES) throw new RequestError(400, 'invalid_request', 'The request is too large.');
  let action;
  try { action = JSON.parse(bytes.toString('utf8')); } catch { throw new RequestError(400, 'invalid_request', 'The request body is invalid.'); }
  if (!action || typeof action !== 'object' || Array.isArray(action)) throw new RequestError(400, 'invalid_request', 'The request body is invalid.');
  const keys = Object.keys(action);
  if (keys.some((key) => !ACTION_FIELDS.includes(key)) || ACTION_FIELDS.slice(0, 7).some((key) => !(key in action))) {
    throw new RequestError(400, 'invalid_request', 'The action is invalid.');
  }
  for (const [field, max] of Object.entries(BOUNDED_STRINGS)) {
    const value = action[field];
    if (typeof value !== 'string' || !value || value.length > max) throw new RequestError(400, 'invalid_request', 'The action is invalid.');
  }
  if (!UUID.test(action.session_id) || !Number.isSafeInteger(action.revision) || action.revision < 1) {
    throw new RequestError(400, 'invalid_request', 'The action is invalid.');
  }
  return { userId: sub, bearerToken: match[1], action: { ...action, value: action.value === undefined ? null : action.value } };
}

/** Dependencies are injectable for tests; the production adapter is below. */
function createHandler(responseFrom, deps) {
  return async (event, rawStream, context = {}) => {
    const requestId = event?.requestContext?.requestId || context.awsRequestId || 'unknown';
    let stage = 'validate';
    const log = (level, outcome, error) => console[level](JSON.stringify({
      event: 'ui_actions', request_id: requestId, stage, outcome,
      ...(error ? { error_type: error.name || 'Error' } : {}),
      ...(error instanceof RequestError ? { code: error.code, status: error.statusCode } : {}),
    }));
    const respond = (statusCode, body) => {
      const stream = responseFrom(rawStream, { statusCode, headers: deps.headers(event) });
      // Lambda writes the HTTP metadata and delimiter on the first write(), not end(body).
      stream.write(JSON.stringify(body));
      stream.end();
    };
    try {
      const parsed = validateRequest(event);
      stage = 'ownership';
      const agentType = await deps.sessionAgent(parsed.userId, parsed.action.session_id);
      if (agentType === null) throw new RequestError(404, 'not_found', 'The session was not found.');
      const agent = RESUMABLE[agentType];
      if (!agent) throw new RequestError(400, 'invalid_request', 'This chat is not waiting for input.');
      stage = 'resume';
      const envelope = await deps.resume(agent, parsed);
      if (!envelope || typeof envelope !== 'object' || Array.isArray(envelope) || !ENVELOPE_STATUSES.has(envelope.status)) {
        throw new Error('invalid runtime response');
      }
      log('info', envelope.status);
      respond(200, envelope);
    } catch (err) {
      const e = err instanceof RequestError ? err : new RequestError(503, 'service_unavailable', 'The action could not be completed. Try again.', true);
      log(e.statusCode >= 500 ? 'error' : 'info', e.statusCode >= 500 ? 'resume_failed' : 'refused', err);
      respond(e.statusCode, { error: { code: e.code, message: e.message, retriable: e.retriable, request_id: requestId } });
    }
  };
}

function productionDependencies() {
  const { DynamoDBClient, GetItemCommand } = require('@aws-sdk/client-dynamodb');
  const db = new DynamoDBClient({});
  const metadata = process.env.CEI_CHAT_METADATA_TABLE;
  const agentUrls = JSON.parse(process.env.AGENT_URLS || '{}');
  const allowedOrigins = new Set((process.env.ALLOWED_ORIGINS || '').split(',').filter(Boolean));
  return {
    headers(event) {
      const origin = Object.entries(event.headers || {}).find(([k]) => k.toLowerCase() === 'origin')?.[1];
      return { 'Content-Type': 'application/json', 'Cache-Control': 'no-store', ...(allowedOrigins.has(origin) ? { 'Access-Control-Allow-Origin': origin, Vary: 'Origin' } : {}) };
    },
    async sessionAgent(userId, sessionId) {
      const r = await db.send(new GetItemCommand({ TableName: metadata, Key: { user_id: { S: userId }, session_id: { S: sessionId } }, ConsistentRead: true, ProjectionExpression: 'agent_type' }));
      if (!r.Item) return null;
      return r.Item.agent_type?.S || '';
    },
    async resume(agent, parsed) {
      const url = new URL(agentUrls[agent]);
      if (url.protocol !== 'https:' || !/^bedrock-agentcore\.[a-z0-9-]+\.amazonaws\.com(?:\.cn)?$/.test(url.hostname) || url.port || url.username || url.password || !/^\/runtimes\/[^/]+\/invocations$/.test(url.pathname) || url.search !== '?qualifier=DEFAULT') throw new Error('invalid runtime URL');
      const response = await fetch(url, {
        method: 'POST', redirect: 'error',
        headers: { Authorization: `Bearer ${parsed.bearerToken}`, 'X-Amzn-Bedrock-AgentCore-Runtime-Session-Id': parsed.action.session_id, Accept: 'application/json', 'Content-Type': 'application/json' },
        body: JSON.stringify({ op: 'resume', action: parsed.action }),
        signal: AbortSignal.timeout(RUNTIME_TIMEOUT_MS),
      });
      if (response.status === 401 || response.status === 403) throw new RequestError(401, 'unauthorized', 'Sign in again to continue.');
      if (!response.ok) throw new Error('runtime unavailable');
      return response.json();
    },
  };
}

if (globalThis.awslambda) exports.handler = globalThis.awslambda.streamifyResponse(createHandler((stream, metadata) => globalThis.awslambda.HttpResponseStream.from(stream, metadata), productionDependencies()));
exports.createHandler = createHandler;
exports.validateRequest = validateRequest;
exports.RESUMABLE = RESUMABLE;
