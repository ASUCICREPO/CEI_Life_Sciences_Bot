import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import {
  AlertCircle,
  ArrowLeft,
  ArrowRight,
  Check,
  Circle,
  ExternalLink,
  LoaderCircle,
  Play,
  Search,
  ShieldCheck,
  Sparkles,
} from 'lucide-react';

import { useProductTour } from '../components/tour/ProductTour';
import { NcbiToolCard } from '../components/NcbiToolCard';
import { ncbiTools } from '../mocks/ncbiTools';
import type { NcbiToolId } from '../mocks/ncbiTools';
import {
  configuredToolSourceIds,
  toolSourceBranding,
  toolSources,
} from '../mocks/tools';
import type { ToolSource, ToolSourceId } from '../mocks/tools';
import { Dna, GitBranch, ListTree } from 'lucide-react';
import { startNewSessionId } from '../lib/agentClient';
import { StreamingSurface } from '../components/streaming/StreamingSurface';
import { useAuth } from '../lib/auth/AuthProvider';
import { useSessionsClient } from '../lib/useSessions';

type RunState = 'idle' | 'running' | 'success' | 'error';

function displayText(value: string) {
  return value
    .replace(/10\.demo\//gi, '10.1000/')
    .replace(/DEMO-/g, '')
    .replace(/\b(mock|demo|sample|illustrative|example)\b[ -]*/gi, '')
    .replace(/\s{2,}/g, ' ')
    .trim();
}

function SourceLogo({ source, muted = false }: { source: ToolSource; muted?: boolean }) {
  const branding = toolSourceBranding[source.id];
  return (
    <div className={`flex h-16 w-20 shrink-0 items-center justify-center rounded-xl border bg-white p-2 ${muted ? 'border-border-subtle grayscale opacity-55' : 'border-border-default'}`}>
      <img src={branding.logoUrl} alt={branding.logoAlt} className="max-h-11 max-w-full object-contain" loading="lazy" />
    </div>
  );
}

const ncbiToolIcon: Record<NcbiToolId, typeof Dna> = {
  'taxonomy-lineage': ListTree,
  'sequence-search': GitBranch,
  'blast-sequence': Dna,
};

export function ToolsPage() {
  const { reportTourEvent } = useProductTour();
  const sessionsClient = useSessionsClient();
  const [selectedNcbiToolId, setSelectedNcbiToolId] = useState<NcbiToolId | null>(null);
  const [selectedSourceId, setSelectedSourceId] = useState<ToolSourceId | null>(null);
  const [query, setQuery] = useState('');
  const [runState, setRunState] = useState<RunState>('idle');
  const [stageIndex, setStageIndex] = useState(0);
  const [streamBody, setStreamBody] = useState<Record<string, unknown> | null>(null);
  const [streamSessionId, setStreamSessionId] = useState<string | null>(null);
  const [agentError, setAgentError] = useState<string | null>(null);
  const timers = useRef<number[]>([]);

  const source = selectedSourceId
    ? toolSources.find((item) => item.id === selectedSourceId) ?? null
    : null;

  const clearTimers = () => {
    timers.current.forEach((timer) => window.clearTimeout(timer));
    timers.current = [];
  };

  useEffect(() => clearTimers, []);

  const handleSourceSelect = (nextSource: ToolSource) => {
    if (!configuredToolSourceIds.includes(nextSource.id)) return;
    clearTimers();
    setSelectedSourceId(nextSource.id);
    setQuery(nextSource.example);
    setRunState('idle');
    setStageIndex(0);
    setStreamBody(null);
    setStreamSessionId(null);
    setAgentError(null);
    reportTourEvent({ type: 'TOOL_SOURCE_SELECTED', id: nextSource.id });
  };

  const handleBack = () => {
    clearTimers();
    setSelectedSourceId(null);
    setQuery('');
    setRunState('idle');
    setStageIndex(0);
    setStreamBody(null);
    setStreamSessionId(null);
    setAgentError(null);
  };

  const handleRun = async (event: FormEvent) => {
    event.preventDefault();
    if (!source || !query.trim() || runState === 'running') return;
    clearTimers();
    setRunState('running');
    setStageIndex(0);
    setStreamBody(null);
    setStreamSessionId(null);
    setAgentError(null);

    // Animate the stages while the session is being created
    const stageInterval = window.setInterval(() => {
      setStageIndex((prev) => Math.min(prev + 1, 3));
    }, 2500);
    timers.current.push(stageInterval);

    try {
      // Create a fresh session for this tool query so it appears in chat history
      const created = await sessionsClient.createSession({
        title: `${source.name}: ${query.slice(0, 80)}`,
        mode: 'science',
      });
      clearTimers();
      const runId = crypto.randomUUID();
      setStreamSessionId(created.session_id);
      setStreamBody({
        session_id: created.session_id,
        run_id: runId,
        message: `Using ${source.name} only: ${query}`,
        task_type: 'live-research',
        mode: 'science',
      });
      setRunState('success');
      reportTourEvent({ type: 'TOOLS_RUN_COMPLETED' });
    } catch (_err) {
      clearTimers();
      setAgentError('Could not start the search. Make sure you are signed in and try again.');
      setRunState('error');
    }
  };


  // ─── NCBI tool detail view ─────────────────────────────────
  if (selectedNcbiToolId) {
    const tool = ncbiTools.find((t) => t.id === selectedNcbiToolId)!;
    return (
      <div className="max-w-4xl mx-auto px-4 sm:px-6 py-6 sm:py-8">
        <button
          type="button"
          onClick={() => setSelectedNcbiToolId(null)}
          className="mb-5 inline-flex items-center gap-1.5 text-xs font-medium text-text-secondary hover:text-cei-blue"
        >
          <ArrowLeft size={13} /> All tools
        </button>

        <header className="mb-5">
          <div className="flex items-center gap-2 mb-2">
            <span className="inline-flex items-center gap-1.5 rounded-full bg-cei-blue/8 px-2.5 py-1 text-[10px] font-semibold uppercase tracking-wider text-cei-blue">
              <Sparkles size={11} aria-hidden="true" /> NCBI tool
            </span>
          </div>
          <h1 className="text-xl font-semibold text-text-primary">{tool.name}</h1>
          <p className="text-sm text-text-secondary mt-1 max-w-2xl">{tool.description}</p>
        </header>

        <NcbiToolCard toolId={tool.id} />

        <div className="mt-5 flex items-start gap-2.5 rounded-lg border border-border-subtle bg-surface-panel/60 px-4 py-3 text-xs text-text-secondary">
          <AlertCircle size={14} className="mt-0.5 shrink-0 text-evidence-moderate" />
          <p><span className="font-semibold text-text-primary">Demo data:</span> results are illustrative and shaped like real {tool.interface} responses. Wire the runner in <span className="font-mono">src/mocks/ncbiTools.ts</span> to the live API for production.</p>
        </div>
      </div>
    );
  }

  if (!source) {
    return (
      <div className="max-w-7xl mx-auto px-4 sm:px-6 py-6 sm:py-8">
        <header data-tour="tools-overview" className="flex flex-col sm:flex-row sm:items-start sm:justify-between gap-4 mb-7">
          <div>
            <div className="flex items-center gap-2 mb-2">
              <span className="inline-flex items-center gap-1.5 rounded-full bg-cei-blue/8 px-2.5 py-1 text-[10px] font-semibold uppercase tracking-wider text-cei-blue">
                <Sparkles size={11} aria-hidden="true" /> Natural-language tools
              </span>
            </div>
            <h1 className="text-xl font-semibold text-text-primary">Choose a data source</h1>
            <p className="text-sm text-text-secondary mt-1 max-w-3xl">
              Start with one of the available sources below. Each tool understands that source’s data, filters, limits, and result structure.
            </p>
          </div>
          <div className="flex items-center gap-2 rounded-lg border border-success/20 bg-success-bg px-3 py-2 text-xs text-success shrink-0">
            <ShieldCheck size={14} aria-hidden="true" /> All {toolSources.length} sources live
          </div>
        </header>

        <section aria-labelledby="ncbi-tools-heading" className="mb-8">
          <div className="mb-4">
            <h2 id="ncbi-tools-heading" className="text-sm font-semibold text-text-primary">NCBI quick tools</h2>
            <p className="text-xs text-text-secondary mt-1">Answer a specific question in one step. Pick a tool, fill in the input, and run it.</p>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            {ncbiTools.map((tool) => {
              const Icon = ncbiToolIcon[tool.id];
              return (
                <button
                  key={tool.id}
                  type="button"
                  onClick={() => setSelectedNcbiToolId(tool.id)}
                  className="group relative rounded-xl border border-border-subtle bg-surface-elevated p-5 text-left shadow-sm transition-all hover:-translate-y-0.5 hover:border-cei-blue-light/50 hover:shadow-md"
                  aria-label={`Open ${tool.name}`}
                >
                  <div className="flex items-start justify-between gap-3">
                    <div className="grid h-11 w-11 place-items-center rounded-xl bg-cei-blue/10 text-cei-blue">
                      <Icon size={20} aria-hidden="true" />
                    </div>
                    <span className="inline-flex items-center gap-1.5 rounded-full bg-success-bg px-2 py-1 text-[10px] font-semibold text-success">
                      <Circle size={6} className="fill-success" /> Ready
                    </span>
                  </div>
                  <h3 className="mt-4 text-sm font-semibold text-text-primary">{tool.name}</h3>
                  <p className="mt-1 text-xs leading-5 text-text-secondary">{tool.question}</p>
                  <div className="mt-4 flex items-center justify-between border-t border-border-subtle pt-3 text-xs text-cei-blue">
                    <span className="font-medium">Open tool</span>
                    <ArrowRight size={14} className="transition-transform group-hover:translate-x-0.5" />
                  </div>
                </button>
              );
            })}
          </div>
        </section>

        <section aria-labelledby="available-tools-heading">
          <div className="flex flex-col sm:flex-row sm:items-end sm:justify-between gap-2 mb-4">
            <div>
              <h2 id="available-tools-heading" className="text-sm font-semibold text-text-primary">Available APIs</h2>
              <p className="text-xs text-text-secondary mt-1">All sources are live and ready to use.</p>
            </div>
            <div className="flex items-center gap-4 text-[10px] font-medium text-text-secondary" aria-label="Connection status legend">

            </div>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
            {toolSources.map((item) => {
              const branding = toolSourceBranding[item.id];
              return (
                <button
                  key={item.id}
                  type="button"
                  data-tour={item.id === 'clinical-trials' ? 'tools-source' : undefined}
                  
                  onClick={() => handleSourceSelect(item)}
                  className="group relative min-h-56 rounded-xl border border-border-subtle bg-surface-elevated p-5 text-left shadow-sm transition-all hover:-translate-y-0.5 hover:border-cei-blue-light/50 hover:shadow-md cursor-pointer"
                  aria-label={`Open ${item.name}`}
                >
                  <div className="flex items-start justify-between gap-3">
                    <SourceLogo source={item} />
                    <span className="inline-flex items-center gap-1.5 rounded-full bg-success-bg px-2 py-1 text-[10px] font-semibold text-success">
                      <Circle size={6} className="fill-success" /> Live
                    </span>
                  </div>

                  <div className="mt-4">
                    <div className="flex items-center gap-2">
                      <h3 className="text-sm font-semibold text-text-primary">{item.name}</h3>
                      <span className="rounded-full bg-surface-panel px-2 py-0.5 text-[9px] font-medium text-text-secondary">{item.category}</span>
                    </div>
                    <p className="mt-2 text-xs leading-5 text-text-secondary">{item.description}</p>
                  </div>

                  <div className="absolute inset-x-5 bottom-4 flex items-center justify-between border-t border-border-subtle pt-3 text-xs text-cei-blue">
                    <span className="font-medium">{item.capability}</span>
                    <ArrowRight size={14} className="transition-transform group-hover:translate-x-0.5" />
                  </div>
                  <span className="sr-only">Official website: {branding.websiteUrl}</span>
                </button>
              );
            })}
          </div>
        </section>

      </div>
    );
  }

  const branding = toolSourceBranding[source.id];

  return (
    <div className="max-w-6xl mx-auto px-4 sm:px-6 py-6 sm:py-8">
      <button type="button" onClick={handleBack} className="mb-5 inline-flex items-center gap-1.5 text-xs font-medium text-text-secondary hover:text-cei-blue">
        <ArrowLeft size={13} /> All tools
      </button>

      <header className="mb-5 flex flex-col gap-4 rounded-xl border border-border-subtle bg-surface-elevated p-5 shadow-sm sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-center gap-4 min-w-0">
          <SourceLogo source={source} />
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="text-xl font-semibold text-text-primary">{source.name}</h1>
              <span className="inline-flex items-center gap-1 rounded-full bg-success-bg px-2 py-0.5 text-[10px] font-semibold text-success"><Circle size={6} className="fill-success" /> Configured</span>
            </div>
            <p className="mt-1 text-sm text-text-secondary">{source.description}</p>
          </div>
        </div>
        <a href={branding.websiteUrl} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1.5 text-xs font-medium text-cei-blue hover:text-cei-navy shrink-0">
          Official website <ExternalLink size={12} />
        </a>
      </header>

      <div className="mb-5 grid grid-cols-1 gap-3 sm:grid-cols-3">
        <div className="rounded-lg border border-border-subtle bg-surface-panel/60 px-4 py-3"><p className="text-[10px] uppercase tracking-wider text-text-tertiary">Search mode</p><p className="mt-1 text-xs font-medium text-text-primary">{source.capability}</p></div>
        <div className="rounded-lg border border-border-subtle bg-surface-panel/60 px-4 py-3"><p className="text-[10px] uppercase tracking-wider text-text-tertiary">Interface</p><p className="mt-1 text-xs font-medium text-text-primary">{displayText(source.interface)}</p></div>
        <div className="rounded-lg border border-border-subtle bg-surface-panel/60 px-4 py-3"><p className="text-[10px] uppercase tracking-wider text-text-tertiary">Usage boundary</p><p className="mt-1 text-xs font-medium text-text-primary">{source.limit}</p></div>
      </div>

      <div className="space-y-5">
        <form onSubmit={handleRun} className="rounded-xl border border-border-subtle bg-surface-elevated p-5 shadow-sm">
          <div className="flex items-start justify-between gap-4 mb-4">
            <div><h2 className="text-sm font-semibold text-text-primary">Ask {source.name}</h2><p className="text-xs text-text-secondary mt-1">Describe the records you need; review the interpreted parameters before using the results.</p></div>
            <Sparkles size={18} className="text-cei-blue shrink-0" aria-hidden="true" />
          </div>
          <label htmlFor="tool-query" className="block text-xs font-medium text-text-secondary mb-2">Natural-language request</label>
          <textarea
            id="tool-query"
            data-tour="tools-query"
            value={query}
            onChange={(event) => { setQuery(event.target.value); if (runState === 'success') setRunState('idle'); }}
            rows={4}
            className="w-full resize-y rounded-xl border border-border-default bg-surface-panel px-4 py-3 text-sm leading-6 text-text-primary placeholder:text-text-tertiary focus:border-cei-blue-light focus:ring-2 focus:ring-cei-blue-light/20"
            placeholder={`Ask a question supported by ${source.name}...`}
          />
          <div className="mt-3 flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
            <button type="button" onClick={() => { setQuery(source.example); setRunState('idle'); }} className="text-left text-xs text-cei-blue hover:text-cei-navy"><span className="font-semibold">Try:</span> “{source.example}”</button>
            <button data-tour="tools-run" type="submit" disabled={!query.trim() || runState === 'running'} className="inline-flex items-center justify-center gap-2 rounded-lg bg-cei-blue px-4 py-2.5 text-sm font-medium text-white hover:bg-cei-navy disabled:cursor-not-allowed disabled:opacity-40 transition-colors shrink-0">
              {runState === 'running' ? <LoaderCircle size={15} className="animate-spin" /> : <Play size={14} />}
              {runState === 'running' ? 'Running search' : 'Run search'}
            </button>
          </div>
        </form>

        <div className="flex items-start gap-2.5 rounded-lg border border-border-subtle bg-surface-panel/60 px-4 py-3 text-xs text-text-secondary">
          <AlertCircle size={14} className="mt-0.5 shrink-0 text-evidence-moderate" /> <p><span className="font-semibold text-text-primary">Source boundary:</span> {source.constraint}</p>
        </div>

        {runState === 'idle' && (
          <section className="rounded-xl border border-dashed border-border-default bg-surface-panel/40 px-6 py-10 text-center">
            <Search size={25} className="mx-auto text-text-tertiary" /><h2 className="mt-3 text-sm font-semibold text-text-primary">Ready to build a source-specific query</h2><p className="mt-1 text-xs text-text-secondary">Run the example or edit it to preview interpreted parameters and matching records.</p>
          </section>
        )}

        {runState === 'running' && (
          <section className="rounded-xl border border-cei-blue/20 bg-surface-elevated p-5 shadow-sm" aria-live="polite" aria-busy="true">
            <div className="flex items-center gap-3 mb-4">
              <div className="grid h-9 w-9 place-items-center rounded-lg bg-cei-blue/8 text-cei-blue"><LoaderCircle size={18} className="animate-spin" /></div>
              <div>
                <h2 className="text-sm font-semibold text-text-primary">Searching {source.name}</h2>
                <p className="text-xs text-text-secondary mt-0.5">
                  {stageIndex === 0 && 'Interpreting your request…'}
                  {stageIndex === 1 && 'Building query plan…'}
                  {stageIndex === 2 && 'Fetching records…'}
                  {stageIndex >= 3 && 'Processing results…'}
                </p>
              </div>
            </div>
            <ol className="grid grid-cols-1 sm:grid-cols-4 gap-2">
              {(['Interpret request', 'Build query', 'Fetch records', 'Process results'] as const).map((stage, index) => {
                const complete = index < stageIndex;
                const active = index === stageIndex;
                return <li key={stage} className={`rounded-lg border p-3 text-xs ${active ? 'border-cei-blue/30 bg-cei-blue/5 text-cei-blue' : complete ? 'border-success/20 bg-success-bg text-success' : 'border-border-subtle text-text-tertiary'}`}><span className="mb-2 grid h-5 w-5 place-items-center rounded-full border border-current">{complete ? <Check size={11} /> : index + 1}</span>{stage}</li>;
              })}
            </ol>
          </section>
        )}

        {runState === 'error' && agentError && (
          <section className="rounded-xl border border-red-200 bg-red-50 p-5" aria-live="polite">
            <div className="flex items-start gap-3">
              <AlertCircle size={18} className="shrink-0 text-red-500 mt-0.5" />
              <div>
                <h2 className="text-sm font-semibold text-red-700">Search failed</h2>
                <p className="mt-1 text-xs text-red-600">{agentError}</p>
              </div>
            </div>
          </section>
        )}

        {runState === 'success' && streamBody && streamSessionId && (
          <div data-tour="tools-results" className="space-y-5">
            <section className="rounded-xl border border-border-subtle bg-surface-elevated shadow-sm overflow-hidden">
              <div className="flex items-center justify-between px-5 py-4 border-b border-border-subtle">
                <div>
                  <p className="text-[10px] font-semibold uppercase tracking-wider text-success">Search complete · live data</p>
                  <h2 className="text-sm font-semibold text-text-primary mt-1">{source.name} results</h2>
                </div>
                <span className="inline-flex items-center gap-1.5 rounded-full bg-success-bg px-2.5 py-1 text-xs font-medium text-success"><Check size={12} /> Live data</span>
              </div>
              <div className="p-5">
                <StreamingSurface sessionId={streamSessionId} agent="research" body={streamBody} />
              </div>
            </section>
          </div>
        )}
      </div>
    </div>
  );
}