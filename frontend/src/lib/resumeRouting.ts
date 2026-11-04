import type { AgentName, AnalysisType, DocumentType } from './agentClient';

// ADR: one pure module owns task <-> agent routing | Rationale: resuming a chat must reopen the runtime that
// owns its session (checkpoint + thread); guessing from the task (e.g. "not analysis => pipeline") silently sent
// Research/Drafting turns to the Pipeline Manager.

export const liveResearchTaskId = 'live-research';
export const livePipelineTaskId = 'run-pipeline';

export const liveAnalysisTaskIds: Readonly<Record<string, AnalysisType>> = {
  'analysis-competitor': 'competitor',
  'analysis-market': 'market',
  'analysis-legal': 'legal_landscape',
};

export const liveDraftingTaskIds: Readonly<Record<string, DocumentType>> = {
  'drafting-grant': 'grant',
  'drafting-vc-pitch': 'vc_pitch',
  'drafting-journal': 'journal',
  'drafting-whitepaper': 'whitepaper',
  'drafting-latex': 'latex',
};

/** Analysis chats saved before analysis_type was stored reopen in the competitor task. */
export const defaultAnalysisTaskId = 'analysis-competitor';

export const liveTaskIds: ReadonlySet<string> = new Set([
  liveResearchTaskId,
  ...Object.keys(liveDraftingTaskIds),
  ...Object.keys(liveAnalysisTaskIds),
  livePipelineTaskId,
]);

/** Stored `agent_type` (stamped by each runtime's begin_turn) -> frontend agent route. */
const agentByAgentType: Readonly<Record<string, AgentName>> = {
  research: 'research',
  drafting: 'drafting',
  analysis: 'analysis',
  pipeline_manager: 'pipeline',
};

function own<T>(table: Readonly<Record<string, T>>, key: string | null | undefined): T | undefined {
  return typeof key === 'string' && Object.prototype.hasOwnProperty.call(table, key) ? table[key] : undefined;
}

function taskFor<T>(table: Readonly<Record<string, T>>, value: string | null | undefined): string | undefined {
  if (typeof value !== 'string') return undefined;
  return Object.keys(table).find((taskId) => table[taskId] === value);
}

/** The agent a live task talks to, or null for a mock (non-live) task. */
export function agentForTask(taskId: string): AgentName | null {
  if (taskId === liveResearchTaskId) return 'research';
  if (own(liveDraftingTaskIds, taskId)) return 'drafting';
  if (own(liveAnalysisTaskIds, taskId)) return 'analysis';
  if (taskId === livePipelineTaskId) return 'pipeline';
  return null;
}

export function agentForAgentType(agentType: string | null | undefined): AgentName | null {
  return own(agentByAgentType, agentType) ?? null;
}

export interface ResumeRouteState {
  agentType?: string | null;
  analysisType?: string | null;
  documentType?: string | null;
}

export type ResumeTarget =
  | { kind: 'resume'; taskId: string; agent: AgentName }
  | { kind: 'unresumable'; agentType: string | null; reason: string };

/** Picks the live task that reopens a saved chat in the runtime that owns it; never falls back to the pipeline. */
export function resolveResumeTarget(state: ResumeRouteState | null | undefined): ResumeTarget {
  const agentType = typeof state?.agentType === 'string' && state.agentType ? state.agentType : null;
  const agent = agentForAgentType(agentType);
  if (agent === null) {
    return {
      kind: 'unresumable',
      agentType,
      reason: agentType ? `Chats from the "${agentType}" agent can't be continued here.` : "This chat doesn't record which agent it belongs to.",
    };
  }
  switch (agent) {
    case 'research':
      return { kind: 'resume', taskId: liveResearchTaskId, agent };
    case 'pipeline':
      return { kind: 'resume', taskId: livePipelineTaskId, agent };
    case 'analysis':
      return { kind: 'resume', taskId: taskFor(liveAnalysisTaskIds, state?.analysisType) ?? defaultAnalysisTaskId, agent };
    case 'drafting': {
      // The Drafting runtime rejects turns without a valid document_type, so there is no safe default.
      const taskId = taskFor(liveDraftingTaskIds, state?.documentType);
      if (taskId) return { kind: 'resume', taskId, agent };
      return { kind: 'unresumable', agentType, reason: "This drafting chat doesn't record its document type." };
    }
  }
}
