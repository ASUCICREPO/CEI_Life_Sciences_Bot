import { safeParseCEIUISurface, type ActionRequest, type ActionResponse, type CEIUISurface } from './cei-ui/schema';
import { actionResponseMatchesRequest } from './cei-ui/action-correlation';

export type AgentName = 'research' | 'drafting' | 'analysis' | 'pipeline';

export type AgentMode = 'science' | 'business';

export type AnalysisType = 'competitor' | 'market' | 'legal_landscape';

export type AgentStatus = 'completed' | 'awaiting_input' | 'running' | 'stale' | 'rejected';

export interface AgentRunPayload {
  prompt: string;
  mode: AgentMode;
  user_id: string;
  analysis_type?: AnalysisType;
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

function agentPath(agent: AgentName): string {
  return `/api/agents/${agent}`;
}

async function postInvocation(agent: AgentName, body: unknown, sessionId: string, timeoutMs: number): Promise<unknown> {
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
      },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
  } catch (error) {
    if (controller.signal.aborted) {
      throw new AgentClientError('The agent did not respond in time.', error);
    }
    throw new AgentClientError('Could not reach the agent service.', error);
  } finally {
    clearTimeout(timeout);
  }

  if (!response.ok) {
    throw new AgentClientError(`The agent service returned an error (HTTP ${response.status}).`);
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
export function getOrCreateSessionId(threadKey: string): string {
  const storageKey = `cei-session:${threadKey}`;
  const existing = sessionStorage.getItem(storageKey);
  if (existing) return existing;
  const created = `cei-demo-${crypto.randomUUID()}`;
  sessionStorage.setItem(storageKey, created);
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
