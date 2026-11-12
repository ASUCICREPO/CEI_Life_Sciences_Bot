import { useEffect, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { useApp } from '../context/AppContext';
import { useProductTour } from '../components/tour/ProductTour';
import { taskGroupsForMode } from '../mocks/tasks';
import { LiveAgentChat } from '../components/LiveAgentChat';
import { ViewMode } from '../design-system/tokens';
import type { AgentName } from '../lib/agentClient';
import {
  agentForTask,
  liveAnalysisTaskIds,
  resolveResumeTarget,
  type ResumeRouteState,
} from '../lib/resumeRouting';
import {
  Scale, GitBranch, Search,
  ArrowRight, Clock, ShieldAlert,
  ArrowLeft, Plus,
} from 'lucide-react';

const groupIcons: Record<string, typeof Scale> = {
  'landscape-analysis': Scale,
  'live-research-drafting': Search,
  'run-a-pipeline': GitBranch,
};

// ADR: every task on this page opens a live agent | lib/resumeRouting.ts owns the task <-> agent map;
// mocks/tasks.ts no longer lists any task that doesn't resolve to one of these agents.
const livePlaceholders: Record<'science' | 'business', Record<AgentName, string>> = {
  science: {
    research: 'Ask a scientific question...',
    drafting: 'Describe the scientific document to draft...',
    analysis: 'Describe the programs, indication, or regulatory scope to analyze...',
    pipeline: 'Describe the multi-step research request...',
  },
  business: {
    research: 'Ask a business question...',
    drafting: 'Describe the business document to draft...',
    analysis: 'Describe the competitor, market, or legal scope to analyze...',
    pipeline: 'Describe the multi-step business request...',
  },
};

export function TaskLauncherPage() {
  const { mode } = useApp();
  const { reportTourEvent, isOpen: tourIsOpen, stage: tourStage } = useProductTour();
  const location = useLocation();
  const navigate = useNavigate();
  const [searchQuery, setSearchQuery] = useState('');
  const [expandedGroup, setExpandedGroup] = useState<string | null>(null);
  // Route state from History/Home/LeftRail: `sessionId` is a real backend session to resume in a live chat.
  const routeState = location.state as ({ sessionId?: string; taskId?: string } & ResumeRouteState) | null;
  const resumeSessionId = routeState?.sessionId;
  // Reopen the chat in the runtime that owns the session; an unknown agent never falls through to the pipeline.
  const resumeTarget = resumeSessionId ? resolveResumeTarget(routeState) : null;
  const resumeTaskId = resumeTarget?.kind === 'resume' ? resumeTarget.taskId : undefined;
  const initialTask = routeState?.taskId ?? resumeTaskId ?? null;
  const [activeTask, setActiveTask] = useState<string | null>(initialTask);
  // The resume applies only to the chat opened from History; leaving it or picking a task starts a new session.
  const [resumeSession, setResumeSession] = useState<string | undefined>(resumeSessionId);
  const [unresumable, setUnresumable] = useState(
    resumeTarget?.kind === 'unresumable' && !routeState?.taskId ? resumeTarget : null,
  );
  const agentMode = mode === ViewMode.BUSINESS ? 'business' : 'science';
  const taskGroups = taskGroupsForMode(agentMode);
  const tourIsGuidingTask = tourIsOpen && ['task-query', 'task-group', 'task-select'].includes(tourStage);

  const filteredGroups = searchQuery
    ? taskGroups.map(group => ({
        ...group,
        tasks: group.tasks.filter(task =>
          task.title.toLowerCase().includes(searchQuery.toLowerCase()) ||
          task.description.toLowerCase().includes(searchQuery.toLowerCase()) ||
          task.examples.some(e => e.toLowerCase().includes(searchQuery.toLowerCase()))
        ),
      })).filter(group => group.tasks.length > 0)
    : taskGroups;

  useEffect(() => {
    if (searchQuery.trim().length < 3 || filteredGroups.length === 0) return;
    const timer = window.setTimeout(() => {
      reportTourEvent({ type: 'TASK_QUERY_ENTERED', query: searchQuery });
    }, 650);
    return () => window.clearTimeout(timer);
  }, [filteredGroups.length, reportTourEvent, searchQuery]);

  const handleTaskSelect = (taskId: string) => {
    reportTourEvent({ type: 'TASK_SELECTED', taskId });
    setResumeSession(undefined);
    setActiveTask(taskId);
  };

  const handleBack = () => {
    setActiveTask(null);
    setResumeSession(undefined);
  };

  // "New chat" and "Just ask" both open the real Ask chat (Session Router); "Just ask" carries the
  // typed text so it is sent as the first turn (LiveAgentChat's autoSendPrompt).
  const handleNewChat = () => navigate('/ask');
  const handleJustAsk = () => {
    const trimmed = searchQuery.trim();
    if (!trimmed) return;
    navigate('/ask', { state: { prompt: trimmed } });
  };

  // ─── RESUME FAILED: the saved chat's agent can't be reopened here ──
  if (unresumable && !activeTask) {
    return (
      <div className="h-full flex items-center justify-center px-6">
        <div role="alert" className="max-w-md text-center">
          <div className="w-12 h-12 rounded-2xl bg-cei-blue/10 flex items-center justify-center mb-4 mx-auto">
            <ShieldAlert size={24} className="text-cei-blue" />
          </div>
          <h2 className="text-base font-semibold text-text-primary">Can't resume this chat</h2>
          <p className="text-sm text-text-secondary mt-1.5">{unresumable.reason}</p>
          <p className="text-xs text-text-tertiary mt-1">Agent: {unresumable.agentType ?? 'unknown'}</p>
          <div className="flex gap-2 justify-center mt-4">
            <button
              onClick={() => navigate('/history')}
              className="text-xs px-3 py-1.5 rounded-full border border-border-subtle text-text-secondary hover:text-cei-blue transition-all"
            >
              Back to History
            </button>
            <button
              onClick={() => setUnresumable(null)}
              className="text-xs px-3 py-1.5 rounded-full border border-border-subtle text-text-secondary hover:text-cei-blue transition-all"
            >
              Browse tasks
            </button>
          </div>
        </div>
      </div>
    );
  }

  // ─── ACTIVE TASK: Live agent (Research / Drafting / Analysis / Pipeline Manager) ──
  const liveAgent = activeTask ? agentForTask(activeTask) : null;
  if (activeTask && liveAgent) {
    const analysisType = liveAnalysisTaskIds[activeTask];
    const taskDef = taskGroups.flatMap((group) => group.tasks).find((task) => task.id === activeTask);
    const chatResumeId = activeTask === resumeTaskId ? resumeSession : undefined;
    return (
      <div className="h-full flex flex-col">
        <div className="border-b border-border-subtle px-6 py-2.5 bg-surface-elevated flex items-center gap-3 flex-shrink-0">
          <button
            onClick={handleBack}
            className="flex items-center gap-1.5 text-xs text-text-secondary hover:text-cei-blue transition-colors"
          >
            <ArrowLeft size={14} />
            Back to tasks
          </button>
        </div>
        <div className="flex-1 overflow-hidden">
          <LiveAgentChat
            key={`${activeTask}:${chatResumeId ?? 'new'}`}
            agent={liveAgent}
            mode={agentMode}
            analysisType={analysisType}
            threadKey={activeTask}
            resumeSessionId={chatResumeId}
            title={taskDef?.title ?? 'Live agent'}
            description={taskDef?.description ?? ''}
            examples={taskDef?.examples ?? []}
            placeholder={livePlaceholders[agentMode][liveAgent]}
          />
        </div>
      </div>
    );
  }

  // ─── TASK PICKER (default view) ──────────────────────────────
  return (
    <div className="max-w-5xl mx-auto px-6 py-8">
      {/* Header */}
      <div data-tour="task-overview" className="mb-6 flex items-start justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold text-text-primary">What do you want to accomplish?</h1>
          <p className="text-sm text-text-secondary mt-1">
            A task is a guided workflow that gives Founder AI a clear goal, relevant tools, and an expected output.
          </p>
        </div>
        <button
          onClick={handleNewChat}
          className="flex items-center gap-2 px-4 py-2.5 rounded-xl bg-cei-blue text-white text-sm font-medium hover:bg-cei-navy transition-colors flex-shrink-0 shadow-sm"
        >
          <Plus size={16} />
          New chat
        </button>
      </div>

      {/* Search / Just ask */}
      <div className="relative mb-8">
        <Search size={16} className="absolute left-4 top-1/2 -translate-y-1/2 text-text-tertiary" />
        <input
          data-tour="task-query"
          type="text"
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          placeholder="Describe what you need, or search tasks..."
          className="w-full pl-11 pr-4 py-3 rounded-xl border border-border-subtle bg-surface-elevated text-sm placeholder:text-text-tertiary focus:border-cei-blue-light focus:ring-2 focus:ring-cei-blue-light/20 transition-all shadow-sm"
          aria-label="Search tasks or describe your need"
        />
        {searchQuery && !tourIsGuidingTask && (
          <div className="absolute right-4 top-1/2 -translate-y-1/2">
            <button
              onClick={handleJustAsk}
              className="px-3 py-1 rounded-lg bg-cei-blue text-white text-xs font-medium hover:bg-cei-navy transition-colors"
            >
              Just ask
            </button>
          </div>
        )}
      </div>

      {/* Task groups */}
      <div className="space-y-4">
        {filteredGroups.map((group, groupIndex) => {
          const Icon = groupIcons[group.id] || Search;
          const isExpanded = expandedGroup === group.id;

          return (
            <div key={group.id} className="border border-border-subtle rounded-xl bg-surface-elevated overflow-hidden">
              {/* Group header */}
              <button
                data-tour={groupIndex === 0 ? 'task-group' : undefined}
                onClick={() => {
                  if (!isExpanded) reportTourEvent({ type: 'TASK_GROUP_EXPANDED', groupId: group.id });
                  setExpandedGroup(isExpanded ? null : group.id);
                }}
                className="w-full flex items-center gap-4 px-5 py-4 hover:bg-surface-panel/50 transition-colors text-left"
                aria-expanded={isExpanded}
              >
                <div className="w-10 h-10 rounded-xl bg-cei-blue/5 flex items-center justify-center flex-shrink-0">
                  <Icon size={20} className="text-cei-blue" />
                </div>
                <div className="flex-1 min-w-0">
                  <h2 className="text-sm font-semibold text-text-primary">{group.title}</h2>
                  <p className="text-xs text-text-secondary mt-0.5">{group.description}</p>
                </div>
                <span className="text-xs text-text-tertiary flex-shrink-0">{group.tasks.length} tasks</span>
                <ArrowRight size={14} className={`text-text-tertiary transition-transform ${isExpanded ? 'rotate-90' : ''}`} />
              </button>

              {/* Tasks */}
              {isExpanded && (
                <div className="border-t border-border-subtle px-5 py-3 space-y-2 bg-surface-panel/30">
                  {group.tasks.map((task, taskIndex) => (
                    <button
                      key={task.id}
                      data-tour={groupIndex === 0 && taskIndex === 0 ? 'task-card' : undefined}
                      onClick={() => handleTaskSelect(task.id)}
                      className="w-full text-left px-4 py-3 rounded-lg border border-border-subtle bg-surface-elevated hover:border-cei-blue-light/40 hover:shadow-sm transition-all group"
                    >
                      <div className="flex items-start justify-between gap-3">
                        <div className="flex-1 min-w-0">
                          <p className="text-sm font-medium text-text-primary group-hover:text-cei-blue transition-colors">
                            {task.title}
                          </p>
                          <p className="text-xs text-text-secondary mt-0.5">{task.description}</p>
                          <div className="flex items-center gap-3 mt-2">
                            {task.estimatedTime && (
                              <span className="flex items-center gap-1 text-[10px] text-text-tertiary">
                                <Clock size={10} /> {task.estimatedTime}
                              </span>
                            )}
                            {task.requiresApproval && (
                              <span className="flex items-center gap-1 text-[10px] text-review-required">
                                <ShieldAlert size={10} /> Requires approval
                              </span>
                            )}
                          </div>
                          {/* Example queries */}
                          <div className="mt-2 flex flex-wrap gap-1.5">
                            {task.examples.slice(0, 2).map((ex, i) => (
                              <span key={i} className="text-[10px] px-2 py-0.5 rounded-full bg-surface-panel border border-border-subtle text-text-tertiary">
                                "{ex.length > 50 ? ex.slice(0, 50) + '...' : ex}"
                              </span>
                            ))}
                          </div>
                        </div>
                        <ArrowRight size={14} className="text-text-tertiary group-hover:text-cei-blue-light mt-1 flex-shrink-0 transition-colors" />
                      </div>
                    </button>
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
