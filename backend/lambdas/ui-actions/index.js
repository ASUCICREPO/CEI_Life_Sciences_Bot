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

// F9/B80: the fixed ChoicePicker contract session-runner's index.js renders; matched here, not inferred
// from agent_type, because the session has no agent yet when the ChoicePicker turn ends it.
const ROUTE_CHOICE_INTERACTION_ID = 'router-choice';
const ROUTE_CHOICE_COMPONENT_ID = 'router-choice';
const ROUTE_CHOICE_ACTION_ID = 'route-choice';
const ROUTE_CHOICE_AGENTS = new Set(['research', 'drafting', 'analysis', 'pipeline_manager']);
const EXPIRED_MESSAGE = 'This request has expired. Start again.';

const isRouteChoice = (action) => action.interaction_id === ROUTE_CHOICE_INTERACTION_ID
  && action.component_id === ROUTE_CHOICE_COMPONENT_ID && action.action_id === ROUTE_CHOICE_ACTION_ID;

function actionResponse(action, status, message = null) {
  const { request_id, session_id, surface_id, revision, interaction_id, component_id, action_id, value } = action;
  return { request_id, session_id, surface_id, revision, interaction_id, component_id, action_id, value, status, message };
}

function mapRouterError(value) {
  const codes = ['invalid_request', 'unauthorized', 'not_found', 'concurrency_limit', 'model_unavailable'];
  const code = codes.includes(value?.code) ? value.code : 'service_unavailable';
  const messages = { concurrency_limit: 'Too many active runs.', model_unavailable: "This chat's model is unavailable." };
  return new RequestError([400, 401, 404, 429].includes(value?.status) ? value.status : 503,
    code, messages[code] || 'The action could not be completed. Try again.', code === 'service_unavailable');
}

/**
 * F9/B80: the user's pick from the Router's ChoicePicker. Verifies ownership and the stored pending
 * choice (CLASSIFIER.md step 4), then starts the chosen agent -- through the same Router lease/token
 * broker the Runner uses -- with the ORIGINAL message, never reclassifying. Returns one JSON envelope,
 * the same contract as a resume (`result?`, `text`, `ui?`, `status`, `action_response`).
 */
async function resumeRouteChoice(parsed, deps, requestId) {
  const action = parsed.action;
  const owner = await deps.sessionAgent(parsed.userId, action.session_id);
  if (owner === null) throw new RequestError(404, 'not_found', 'The session was not found.');
  const pending = await deps.pendingChoice(parsed.userId, action.session_id);
  const now = Math.floor(Date.now() / 1000);
  if (!pending || pending.expires_at <= now) {
    return { action_response: actionResponse(action, 'rejected', EXPIRED_MESSAGE), status: 'rejected', code: 'expired' };
  }
  if (action.surface_id !== pending.surface_id || action.revision !== 1
      || typeof action.value !== 'string' || !ROUTE_CHOICE_AGENTS.has(action.value)) {
    return { action_response: actionResponse(action, 'stale', 'This request is no longer waiting for that input.'), status: 'stale' };
  }
  const chosenAgent = action.value;
  const runId = deps.newRunId();
  const route = await deps.routeStart(parsed.bearerToken, runId, action.session_id, chosenAgent, pending, requestId);
  try {
    const envelope = await deps.invokeAgentRun(route, chosenAgent, pending, parsed);
    const status = envelope.status || (typeof envelope.text === 'string' ? 'completed' : undefined);
    if (!status) throw new Error('invalid runtime response');
    await deps.clearPendingChoice(parsed.userId, action.session_id);
    return { ...envelope, status, action_response: actionResponse(action, 'completed') };
  } finally {
    await deps.routeFinish(parsed.bearerToken, action.session_id, runId);
  }
}

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
      if (isRouteChoice(parsed.action)) {
        // F9/B80: a ChoicePicker pick is not a graph resume -- the session has no agent_type yet -- so it
        // is handled before the RESUMABLE/agent_type lookup below, which only applies once an agent has run.
        stage = 'route_choice';
        const envelope = await resumeRouteChoice(parsed, deps, requestId);
        log('info', envelope.status);
        respond(200, envelope);
        return;
      }
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
  const crypto = require('crypto');
  const { DynamoDBClient, GetItemCommand, DeleteItemCommand } = require('@aws-sdk/client-dynamodb');
  const { LambdaClient, InvokeCommand } = require('@aws-sdk/client-lambda');
  const db = new DynamoDBClient({});
  const lambda = new LambdaClient({});
  const metadata = process.env.CEI_CHAT_METADATA_TABLE;
  const activeRuns = process.env.CEI_ACTIVE_RUNS_TABLE;
  const agentUrls = JSON.parse(process.env.AGENT_URLS || '{}');
  const allowedOrigins = new Set((process.env.ALLOWED_ORIGINS || '').split(',').filter(Boolean));
  const pendingChoiceKey = (userId, sessionId) => ({ user_id: { S: userId }, record: { S: `route_choice#${sessionId}` } });
  async function invokeRouter(payload) {
    const result = await lambda.send(new InvokeCommand({ FunctionName: process.env.CEI_ROUTER_FUNCTION, Payload: Buffer.from(JSON.stringify(payload)) }));
    if (result.FunctionError) throw new RequestError(503, 'service_unavailable', 'The action could not be completed. Try again.', true);
    let value; try { value = JSON.parse(Buffer.from(result.Payload || []).toString()); } catch { throw new RequestError(503, 'service_unavailable', 'The action could not be completed. Try again.', true); }
    return value;
  }
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
    newRunId() { return crypto.randomUUID(); },
    // F9: route_choice records live in the Active-Run table (handler.py's store_pending_choice), the same
    // table the Router's own realtime/background leases use -- not a new table.
    async pendingChoice(userId, sessionId) {
      const r = await db.send(new GetItemCommand({ TableName: activeRuns, Key: pendingChoiceKey(userId, sessionId), ConsistentRead: true }));
      if (!r.Item) return null;
      return { message: r.Item.message?.S, mode: r.Item.mode?.S, surface_id: r.Item.surface_id?.S, expires_at: Number(r.Item.expires_at?.N || 0) };
    },
    async clearPendingChoice(userId, sessionId) {
      await db.send(new DeleteItemCommand({ TableName: activeRuns, Key: pendingChoiceKey(userId, sessionId) }));
    },
    async routeStart(bearerToken, runId, sessionId, agent, pending, requestId) {
      const value = await invokeRouter({ op: 'start', bearer_token: bearerToken, request_id: requestId,
        request: { session_id: sessionId, run_id: runId, message: pending.message, mode: pending.mode, route_agent: agent } });
      if (value.status !== 200 || !value.runtime_url) throw mapRouterError(value);
      return value;
    },
    async routeFinish(bearerToken, sessionId, runId) {
      // Best-effort: the lease also expires on its own, so a failed release here is not fatal.
      try { await invokeRouter({ op: 'finish', bearer_token: bearerToken, session_id: sessionId, run_id: runId }); } catch { /* ignore */ }
    },
    async invokeAgentRun(route, agent, pending, parsed) {
      const url = new URL(route.runtime_url);
      if (url.protocol !== 'https:' || !/^bedrock-agentcore\.[a-z0-9-]+\.amazonaws\.com(?:\.cn)?$/.test(url.hostname) || url.port || url.username || url.password || !/^\/runtimes\/[^/]+\/invocations$/.test(url.pathname) || url.search !== '?qualifier=DEFAULT') throw new Error('invalid runtime URL');
      // ADR: default document_type for a free-text drafting route | Rationale: drafting_main.py requires a
      // valid DocumentType and the stored pending choice carries none; mirrors session-runner's own default.
      const body = agent === 'drafting' ? { prompt: pending.message, mode: pending.mode, document_type: 'whitepaper' } : { prompt: pending.message, mode: pending.mode };
      const response = await fetch(url, {
        method: 'POST', redirect: 'error',
        headers: { Authorization: `Bearer ${parsed.bearerToken}`, 'X-Amzn-Bedrock-AgentCore-Runtime-Session-Id': parsed.action.session_id,
          ...(agent === 'research' ? { 'X-Cei-Gateway-Token': route.gateway_token } : {}), Accept: 'application/json', 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(RUNTIME_TIMEOUT_MS),
      });
      if (response.status === 401 || response.status === 403) throw new RequestError(401, 'unauthorized', 'Sign in again to continue.');
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
exports.RESUMABLE = RESUMABLE;
exports.isRouteChoice = isRouteChoice;
exports.actionResponse = actionResponse;
exports.ROUTE_CHOICE_INTERACTION_ID = ROUTE_CHOICE_INTERACTION_ID;
exports.ROUTE_CHOICE_COMPONENT_ID = ROUTE_CHOICE_COMPONENT_ID;
exports.ROUTE_CHOICE_ACTION_ID = ROUTE_CHOICE_ACTION_ID;
exports.mapRouterError = mapRouterError;
