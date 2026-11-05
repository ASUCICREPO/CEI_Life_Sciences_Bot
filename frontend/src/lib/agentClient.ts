import { safeParseCEIUISurface, type ActionRequest, type ActionResponse, type CEIUISurface } from './cei-ui/schema';
import { actionResponseMatchesRequest } from './cei-ui/action-correlation';
import { getLoadedRuntimeConfig } from './auth/config';

export type AgentName = 'research' | 'drafting' | 'analysis' | 'pipeline';

export type AgentMode = 'science' | 'business';

export type AnalysisType = 'competitor' | 'market' | 'legal_landscape';

/** Mirrors backend/agents/drafting/state.py DocumentType; the Drafting runtime rejects any other value. */
export type DocumentType = 'grant' | 'vc_pitch' | 'journal' | 'whitepaper' | 'latex';

export type AgentStatus = 'completed' | 'awaiting_input' | 'running' | 'stale' | 'rejected';

export interface AgentRunPayload {
  prompt: string;
  mode: AgentMode;
  analysis_type?: AnalysisType;
  document_type?: DocumentType;
}

/** Builds a run payload carrying only the turn context the target agent understands. */
export function buildRunPayload(
  agent: AgentName,
  input: { prompt: string; mode: AgentMode; analysisType?: AnalysisType; documentType?: DocumentType },
): AgentRunPayload {
  const payload: AgentRunPayload = { prompt: input.prompt, mode: input.mode };
  if (agent === 'analysis' && input.analysisType) payload.analysis_type = input.analysisType;
  if (agent === 'drafting' && input.documentType) payload.document_type = input.documentType;
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

async function postInvocation(agent: AgentName, body: unknown, sessionId: string, timeoutMs: number): Promise<unknown> {
  const token = tokenProvider ? await tokenProvider() : null;
  // Never send an unauthenticated request: identity comes only from the bearer token's `sub`.
  if (!token) {
    throw new AgentClientError('You are signed out. Sign in again to continue.');
  }
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  let response: Response;
  try {
    response = await fetch(agentPath(agent), {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        accept: 'application/json',
        [SESSION_HEADER]: sessionId,
        authorization: `Bearer ${token}`,
      },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
  } catch (error) {
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

  const parsed: unknown = await response.json().catch((error: unknown) => {
    throw new AgentClientError('The agent service returned an invalid response.', error);
  });
  return parsed;
}

function toAgentEnvelope(raw: unknown): AgentEnvelope {
  if (typeof raw !== 'object' || raw === null) {
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

export async function sendAction(agent: AgentName, action: ActionRequest, sessionId: string): Promise<AgentEnvelope> {
  const raw = await postInvocation(agent, { op: 'resume', action }, sessionId, RUN_TIMEOUT_MS);
  const envelope = toAgentEnvelope(raw);
  if (envelope.action_response && !actionResponseMatchesRequest(action, envelope.action_response)) {
    throw new AgentClientError('The action response did not match the request.');
  }
  return envelope;
}

export async function getJob(jobId: string, sessionId: string): Promise<JobEnvelope> {
  const raw = await postInvocation('pipeline', { op: 'job_status', job_id: jobId }, sessionId, JOB_STATUS_TIMEOUT_MS);
  if (typeof raw !== 'object' || raw === null) {
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
