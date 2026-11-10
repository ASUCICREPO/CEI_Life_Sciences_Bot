import { CheckCircle2, Circle, Info, RotateCcw } from 'lucide-react';
import { useProductTour } from '../components/tour/ProductTour';
import { toolSources } from '../mocks/tools';

export function SettingsPage() {
  const { startTour } = useProductTour();

  return (
    <div className="max-w-3xl mx-auto px-6 py-8">
      <div data-tour="settings-overview" className="flex items-start justify-between gap-4 mb-6">
        <div>
          <h1 className="text-xl font-semibold text-text-primary">Settings</h1>
          <p className="text-sm text-text-secondary mt-1">Manage platform connections, data sources, and privacy controls.</p>
        </div>
        <button onClick={startTour} className="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg border border-border-default text-xs font-medium text-text-secondary hover:bg-surface-panel transition-colors shrink-0">
          <RotateCcw size={13} /> Replay tour
        </button>
      </div>

      {/* AI Providers */}
      <section className="border border-border-subtle rounded-lg p-5 bg-surface-elevated mb-4">
        <h2 className="text-sm font-semibold text-text-primary mb-4">AI providers</h2>
        <div className="space-y-3">
          {[
            { name: 'AWS Bedrock (Claude)', status: 'connected', detail: 'Primary inference provider — Sonnet 4.6 active' },
            { name: 'AWS Bedrock (OpenAI GPT)', status: 'connected', detail: 'Available via Bedrock inference profiles' },
            { name: 'AWS Bedrock (Amazon Nova)', status: 'connected', detail: 'Available via Bedrock inference profiles' },
          ].map((p) => (
            <div key={p.name} className="flex items-center justify-between py-2">
              <div className="flex items-center gap-3">
                <CheckCircle2 size={16} className="text-success shrink-0" />
                <div>
                  <p className="text-sm font-medium text-text-primary">{p.name}</p>
                  <p className="text-xs text-text-secondary">{p.detail}</p>
                </div>
              </div>
            </div>
          ))}
        </div>
      </section>

      {/* Data sources */}
      <section className="border border-border-subtle rounded-lg p-5 bg-surface-elevated mb-4">
        <div className="flex items-start justify-between gap-4 mb-4">
          <div>
            <h2 className="text-sm font-semibold text-text-primary">Data source connections</h2>
            <p className="text-xs text-text-secondary mt-0.5">All sources are live. Sources that accept optional API keys can be configured for higher rate limits.</p>
          </div>
          <span className="inline-flex items-center gap-1.5 rounded-full bg-success-bg px-2.5 py-1 text-[10px] font-semibold text-success shrink-0">
            <Circle size={6} className="fill-success" /> {toolSources.length} live
          </span>
        </div>
        <div className="space-y-1">
          {toolSources.map((source) => {
            const supportsApiKey = source.id === 'pubmed' || source.id === 'openfda';
            return (
              <div key={source.id} className="flex items-center justify-between gap-4 py-2 border-b border-border-subtle last:border-0">
                <div className="min-w-0">
                  <p className="text-sm text-text-primary">{source.name}</p>
                  <p className="text-[10px] text-text-tertiary truncate">{source.interface}</p>
                </div>
                <div className="flex items-center gap-3 shrink-0">
                  {supportsApiKey ? (
                    <>
                      <span className="flex items-center gap-1 text-xs text-success">
                        <Circle size={6} className="fill-success" /> Live · optional key
                      </span>
                      <button
                        type="button"
                        title="API key support coming in a future update"
                        className="text-xs font-medium text-cei-blue-light hover:text-cei-blue flex items-center gap-1"
                        onClick={() => alert('Per-user API keys are coming in a future update. This will let you use your own NCBI / openFDA key for higher rate limits.')}
                      >
                        Add key <Info size={11} />
                      </button>
                    </>
                  ) : (
                    <span className="flex items-center gap-1 text-xs text-success">
                      <CheckCircle2 size={13} className="text-success" /> Live
                    </span>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </section>

      {/* Confidentiality */}
      <section className="border border-border-subtle rounded-lg p-5 bg-surface-elevated">
        <h2 className="text-sm font-semibold text-text-primary mb-4">Data handling and confidentiality</h2>
        <div className="space-y-3 text-sm">
          <div className="flex items-center justify-between">
            <span className="text-text-secondary">Default confidentiality level for new sessions</span>
            <select className="text-xs border border-border-default rounded-md px-2 py-1 bg-white">
              <option>Internal only</option>
              <option>Shareable with partners</option>
              <option>Public</option>
            </select>
          </div>
          <div className="flex items-center justify-between">
            <span className="text-text-secondary">Require review before external export</span>
            <input type="checkbox" defaultChecked className="accent-cei-blue" />
          </div>
          <div className="flex items-center justify-between">
            <span className="text-text-secondary">Log all data access events</span>
            <input type="checkbox" defaultChecked className="accent-cei-blue" />
          </div>
        </div>
      </section>

    </div>
  );
}
