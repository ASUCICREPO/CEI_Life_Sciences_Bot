import { safeParseCEIUISurface, type ActionRequest, type ActionResponse, type CEIUISurface } from './cei-ui/schema';
import { actionResponseMatchesRequest } from './cei-ui/action-correlation';
import { getLoadedRuntimeConfig } from './auth/config';
import { reportClientFailure } from './clientDiagnostics';
import { consumeEventStream } from './stream/openStream';
import type { StreamEvent } from './stream/events';

export type AgentName = 'research' | 'drafting' | 'analysis' | 'pipeline';

export type AgentMode = 'science' | 'business';

export type AnalysisType = 'competitor' | 'market' | 'legal_landscape';

export type AgentStatus = 'completed' | 'awaiting_input' | 'running' | 'stale' | 'rejected';

export interface AgentRunPayload {
  prompt: string;
  mode: AgentMode;
  analysis_type?: AnalysisType;
}

// ADR: never send document_type | the Drafting runtime now infers the document type (and file formats)
// from the prompt itself, so the frontend has nothing to pick or persist.
/** Builds a run payload carrying only the turn context the target agent understands. */
export function buildRunPayload(
  agent: AgentName,
  input: { prompt: string; mode: AgentMode; analysisType?: AnalysisType },
): AgentRunPayload {
  const payload: AgentRunPayload = { prompt: input.prompt, mode: input.mode };
  if (agent === 'analysis' && input.analysisType) payload.analysis_type = input.analysisType;
  return payload;
}

export interface StepStatus {
  step_id: string;
  agent: string;
  description: string;
  status: 'idle' | 'running' | 'completed' | 'partial' | 'failed';
  error?: string | null;
  detail?: string | null;
}

export interface AgentEnvelope {
  result?: unknown;
  text: string;
  ui?: CEIUISurface;
  ui_error?: string;
  status: AgentStatus;
  job_id?: string;
  action_response?: ActionResponse;
}

export interface JobEnvelope {
  job_id: string;
  session_id: string;
  agent_type: string;
  status: 'pending' | 'running' | 'awaiting_approval' | 'completed' | 'partial' | 'failed' | 'cancelled';
  progress_pct: number;
  updated_at: string;
  revision: number;
  steps: StepStatus[];
  text: string;
  ui?: CEIUISurface;
  ui_error?: string;
}

const SESSION_HEADER = 'X-Amzn-Bedrock-AgentCore-Runtime-Session-Id';
const RUN_TIMEOUT_MS = 600_000;
const JOB_STATUS_TIMEOUT_MS = 15_000;

export class AgentClientError extends Error {
  cause?: unknown;
  constructor(message: string, cause?: unknown) {
    super(message);
    this.name = 'AgentClientError';
    this.cause = cause;
  }
}

// ADR: agent proxy URL from /config.json `agentUrls` when present | Rationale: the deployed site POSTs to the
// AgentProxy Lambda function URL (CORS handled by the Lambda service), which forwards the user's bearer token to
// the AgentCore runtime; local runs have no agentUrls and keep the relative path the Vite dev proxy forwards to
// `agentcore dev`.
function agentPath(agent: AgentName): string {
  return getLoadedRuntimeConfig()?.agentUrls?.[agent] ?? `/api/agents/${agent}`;
}

// ADR: injected token provider, set once by the auth layer | Rationale: the runtimes persist and scope chat
// history by the caller's Cognito `sub`, which they read from this bearer token (never from the payload)
let tokenProvider: (() => Promise<string | null>) | null = null;

export function setAgentTokenProvider(provider: (() => Promise<string | null>) | null): void {
  tokenProvider = provider;
}

interface PostTarget {
  /** Absolute or same-origin URL; defaults to the agent proxy for `agent`. */
  url?: string;
  /** The AgentCore session header; the API Gateway CORS allowlist does not include it, so /ui-actions omits it. */
  sessionHeader?: boolean;
  /** Overrides the default `application/json` Accept header (e.g. to request SSE on a drafting action). */
  accept?: string;
}

// ADR: fetch + auth + status handling factored out of postInvocation | sendActionExpectingStream needs the raw
// Response (to read its Content-Type) before deciding whether to parse JSON or consume an event stream.
async function performRequest(
  agent: AgentName,
  body: unknown,
  sessionId: string,
  timeoutMs: number,
  target: PostTarget = {},
): Promise<Response> {
  let token: string | null;
  try { token = tokenProvider ? await tokenProvider() : null; }
  catch (error) { reportClientFailure('auth', 'auth_error'); throw error; }
  // Never send an unauthenticated request: identity comes only from the bearer token's `sub`.
  if (!token) {
    throw new AgentClientError('You are signed out. Sign in again to continue.');
  }
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  let response: Response;
  try {
    response = await fetch(target.url ?? agentPath(agent), {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        accept: target.accept ?? 'application/json',
        ...(target.sessionHeader === false ? {} : { [SESSION_HEADER]: sessionId }),
        authorization: `Bearer ${token}`,
      },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
  } catch (error) {
    reportClientFailure('agent', controller.signal.aborted ? 'timeout' : 'network');
    if (controller.signal.aborted) {
      throw new AgentClientError('The agent did not respond in time.', error);
    }
    // fetch() rejects with a TypeError (no status, no body) when the browser blocks the response (CORS) or the
    // host is unreachable.
    if (error instanceof TypeError) {
      throw new AgentClientError(
        `The ${agent} agent could not be reached: the request was blocked or the service is unreachable ` +
          '(CORS, expired sign-in, throttling or network). Sign in again and retry; if it persists, check the ' +
          'browser console for the blocked request.',
        error,
      );
    }
    throw new AgentClientError('Could not reach the agent service.', error);
  } finally {
    clearTimeout(timeout);
  }

  if (!response.ok) {
    if (response.status >= 500 || response.status === 429) reportClientFailure('agent', 'http_error', response.status);
    if (response.status === 401) {
      throw new AgentClientError('Your sign-in has expired. Sign in again to continue.');
    }
    if (response.status === 429) {
      throw new AgentClientError(`The ${agent} agent is busy. Wait a moment and try again.`);
    }
    if (response.status === 504) {
      throw new AgentClientError('The agent did not respond in time.');
    }
    throw new AgentClientError(`The ${agent} agent returned an error (HTTP ${response.status}).`);
  }

  return response;
}

async function postInvocation(
  agent: AgentName,
  body: unknown,
  sessionId: string,
  timeoutMs: number,
  target: PostTarget = {},
): Promise<unknown> {
  const response = await performRequest(agent, body, sessionId, timeoutMs, target);
  const parsed: unknown = await response.json().catch((error: unknown) => {
    reportClientFailure('agent', 'invalid_response');
    throw new AgentClientError('The agent service returned an invalid response.', error);
  });
  return parsed;
}

function toAgentEnvelope(raw: unknown): AgentEnvelope {
  if (typeof raw !== 'object' || raw === null) {
    reportClientFailure('agent', 'invalid_response');
    throw new AgentClientError('The agent service returned an unexpected response shape.');
  }
  const candidate = raw as Record<string, unknown>;
  const text = typeof candidate.text === 'string' ? candidate.text : '';
  const status = (typeof candidate.status === 'string' ? candidate.status : 'completed') as AgentStatus;
  const envelope: AgentEnvelope = { result: candidate.result, text, status };
  if (typeof candidate.job_id === 'string') envelope.job_id = candidate.job_id;
  if (candidate.action_response) envelope.action_response = candidate.action_response as ActionResponse;

  if (candidate.ui !== undefined && candidate.ui !== null) {
    const parsedUi = safeParseCEIUISurface(candidate.ui);
    if (parsedUi.success) {
      envelope.ui = parsedUi.data;
    } else {
      reportClientFailure('agent', 'invalid_response');
      envelope.ui_error = 'The agent returned a rich view that could not be validated.';
    }
  } else if (typeof candidate.ui_error === 'string') {
    envelope.ui_error = candidate.ui_error;
  }

  return envelope;
}

export async function invokeAgent(agent: AgentName, payload: AgentRunPayload, sessionId: string): Promise<AgentEnvelope> {
  const raw = await postInvocation(agent, { op: 'run', ...payload }, sessionId, RUN_TIMEOUT_MS);
  return toAgentEnvelope(raw);
}

// ADR: deployed builds send actions to POST /ui-actions (F13) | Rationale: the API checks session ownership and
// picks the owning runtime from the session's stored agent, not from the browser; the local demo has no API and
// keeps the agent proxy's op:"resume" path
export async function sendAction(agent: AgentName, action: ActionRequest, sessionId: string): Promise<AgentEnvelope> {
  const apiBaseUrl = getLoadedRuntimeConfig()?.apiBaseUrl;
  const raw = apiBaseUrl
    ? await postInvocation(agent, action, sessionId, RUN_TIMEOUT_MS, { url: `${apiBaseUrl}/ui-actions`, sessionHeader: false })
    : await postInvocation(agent, { op: 'resume', action }, sessionId, RUN_TIMEOUT_MS);
  const envelope = toAgentEnvelope(raw);
  if (envelope.action_response && !actionResponseMatchesRequest(action, envelope.action_response)) {
    reportClientFailure('agent', 'invalid_response');
    throw new AgentClientError('The action response did not match the request.');
  }
  return envelope;
}

export type ActionDispatchResult =
  | { kind: 'envelope'; envelope: AgentEnvelope }
  | { kind: 'stream'; events: AsyncIterable<StreamEvent> };

// ADR: a drafting action may answer over SSE instead of JSON | the outline-approval action starts the full
// draft; the backend decides per-response (Content-Type), so this only exists where /ui-actions is reachable
// (deployed builds) and the caller opts in (LiveAgentChat gates this to drafting chats with streaming on).
export async function sendActionExpectingStream(agent: AgentName, action: ActionRequest, sessionId: string): Promise<ActionDispatchResult> {
  const apiBaseUrl = getLoadedRuntimeConfig()?.apiBaseUrl;
  if (!apiBaseUrl) {
    // The local agent-proxy demo has no /ui-actions endpoint and never streams an action response.
    return { kind: 'envelope', envelope: await sendAction(agent, action, sessionId) };
  }
  const response = await performRequest(agent, action, sessionId, RUN_TIMEOUT_MS, {
    url: `${apiBaseUrl}/ui-actions`,
    sessionHeader: false,
    accept: 'text/event-stream, application/json',
  });
  const contentType = response.headers.get('content-type') ?? '';
  if (contentType.includes('text/event-stream') && response.body) {
    return { kind: 'stream', events: consumeEventStream(response) };
  }
  const raw: unknown = await response.json().catch((error: unknown) => {
    reportClientFailure('agent', 'invalid_response');
    throw new AgentClientError('The agent service returned an invalid response.', error);
  });
  const envelope = toAgentEnvelope(raw);
  if (envelope.action_response && !actionResponseMatchesRequest(action, envelope.action_response)) {
    reportClientFailure('agent', 'invalid_response');
    throw new AgentClientError('The action response did not match the request.');
  }
  return { kind: 'envelope', envelope };
}

export async function getJob(jobId: string, sessionId: string): Promise<JobEnvelope> {
  const raw = await postInvocation('pipeline', { op: 'job_status', job_id: jobId }, sessionId, JOB_STATUS_TIMEOUT_MS);
  if (typeof raw !== 'object' || raw === null) {
    reportClientFailure('agent', 'invalid_response');
    throw new AgentClientError('The job status service returned an unexpected response shape.');
  }
  const candidate = raw as Record<string, unknown>;
  const steps = Array.isArray(candidate.steps) ? (candidate.steps as StepStatus[]) : [];
  const job: JobEnvelope = {
    job_id: String(candidate.job_id ?? jobId),
    session_id: String(candidate.session_id ?? sessionId),
    agent_type: typeof candidate.agent_type === 'string' ? candidate.agent_type : 'pipeline',
    status: (typeof candidate.status === 'string' ? candidate.status : 'pending') as JobEnvelope['status'],
    progress_pct: typeof candidate.progress_pct === 'number' ? candidate.progress_pct : 0,
    updated_at: typeof candidate.updated_at === 'string' ? candidate.updated_at : '',
    revision: typeof candidate.revision === 'number' ? candidate.revision : 1,
    steps,
    text: typeof candidate.text === 'string' ? candidate.text : '',
  };
  if (candidate.ui !== undefined && candidate.ui !== null) {
    const parsedUi = safeParseCEIUISurface(candidate.ui);
    if (parsedUi.success) {
      job.ui = parsedUi.data;
    } else {
      reportClientFailure('agent', 'invalid_response');
      job.ui_error = 'The job returned a rich view that could not be validated.';
    }
  }
  return job;
}

// ADR: one sessionStorage-backed id per chat thread | the backend contract
// ties HITL resume and job polling to one AgentCore runtime session id.
// Every entry point (agent proxy, runtimes, sessions API) accepts only a UUID (8-4-4-4-12 hex) and never
// substitutes a default, so a stored value in any other shape (e.g. a pre-UUID id) is replaced.
const SESSION_UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function isSessionUuid(value: unknown): value is string {
  return typeof value === 'string' && SESSION_UUID.test(value);
}

export function getOrCreateSessionId(threadKey: string): string {
  const storageKey = `cei-session:${threadKey}`;
  const existing = sessionStorage.getItem(storageKey);
  if (isSessionUuid(existing)) return existing;
  const created = crypto.randomUUID();
  sessionStorage.setItem(storageKey, created);
  return created;
}

// ADR: a fresh session id per new chat | reusing one id per task merged every chat on that task into a
// single History thread. A resumed chat passes its own id instead and never writes it back here.
export function startNewSessionId(threadKey: string): string {
  const created = crypto.randomUUID();
  sessionStorage.setItem(`cei-session:${threadKey}`, created);
  return created;
}

// ADR: carry the session id to /pipeline?job=... via sessionStorage | the
// plan allows route state or sessionStorage; sessionStorage also survives a
// page refresh on the pipeline page while a job is still running.
export function setJobSession(jobId: string, sessionId: string): void {
  sessionStorage.setItem(`cei-job-session:${jobId}`, sessionId);
}

export function getJobSession(jobId: string): string | null {
  return sessionStorage.getItem(`cei-job-session:${jobId}`);
}
