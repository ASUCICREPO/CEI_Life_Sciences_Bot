import { describe, expect, it } from 'vitest';
import {
  agentForAgentType,
  agentForTask,
  liveAnalysisTaskIds,
  liveDraftingTaskId,
  livePipelineTaskId,
  liveResearchTaskId,
  liveTaskIds,
  resolveResumeTarget,
} from './resumeRouting';

describe('resumeRouting', () => {
  it('maps every stored agent_type to its agent', () => {
    expect(agentForAgentType('research')).toBe('research');
    expect(agentForAgentType('drafting')).toBe('drafting');
    expect(agentForAgentType('analysis')).toBe('analysis');
    expect(agentForAgentType('pipeline_manager')).toBe('pipeline');
    expect(agentForAgentType('pipeline')).toBeNull();
    expect(agentForAgentType('constructor')).toBeNull();
    expect(agentForAgentType(null)).toBeNull();
  });

  it('maps every live task to its agent and mock tasks to null', () => {
    expect(agentForTask(liveResearchTaskId)).toBe('research');
    expect(agentForTask(liveDraftingTaskId)).toBe('drafting');
    for (const taskId of Object.keys(liveAnalysisTaskIds)) expect(agentForTask(taskId)).toBe('analysis');
    expect(agentForTask(livePipelineTaskId)).toBe('pipeline');
    expect(agentForTask('research-question')).toBeNull();
    for (const taskId of liveTaskIds) expect(agentForTask(taskId)).not.toBeNull();
  });

  it('resumes research and pipeline chats in their own tasks', () => {
    expect(resolveResumeTarget({ agentType: 'research' })).toEqual({ kind: 'resume', taskId: 'live-research', agent: 'research' });
    expect(resolveResumeTarget({ agentType: 'pipeline_manager' })).toEqual({ kind: 'resume', taskId: 'run-pipeline', agent: 'pipeline' });
  });

  it.each(Object.entries(liveAnalysisTaskIds))('resumes analysis task %s from its stored analysis_type', (taskId, analysisType) => {
    expect(resolveResumeTarget({ agentType: 'analysis', analysisType })).toEqual({ kind: 'resume', taskId, agent: 'analysis' });
  });

  it.each([null, undefined, 'bogus'])('falls back to the competitor task for analysis_type %s', (analysisType) => {
    expect(resolveResumeTarget({ agentType: 'analysis', analysisType })).toEqual({
      kind: 'resume', taskId: 'analysis-competitor', agent: 'analysis',
    });
  });

  it.each([null, undefined, 'bogus', 'grant'])('resumes any drafting chat in the one drafting task regardless of document_type %s', (documentType) => {
    expect(resolveResumeTarget({ agentType: 'drafting', documentType })).toEqual({
      kind: 'resume', taskId: liveDraftingTaskId, agent: 'drafting',
    });
  });

  it.each([null, undefined, '', 'foo', 'pipeline'])('treats agent_type %s as unresumable, never the pipeline', (agentType) => {
    const target = resolveResumeTarget({ agentType });
    expect(target.kind).toBe('unresumable');
    expect(JSON.stringify(target)).not.toContain('run-pipeline');
  });

  it('treats missing route state as unresumable', () => {
    expect(resolveResumeTarget(null)).toMatchObject({ kind: 'unresumable', agentType: null });
  });
});
