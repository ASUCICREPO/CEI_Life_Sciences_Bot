import type { Metric, StatGridComponent } from '../../lib/cei-ui/schema';
import { assertNever } from './util';

export function StatGridView({ component }: { component: StatGridComponent }) {
  return (
    <section className="my-3" aria-label={component.title ?? 'Metrics'}>
      {component.title && <h3 className="text-sm font-semibold text-text-primary mb-2">{component.title}</h3>}
      <dl className="grid grid-cols-2 sm:grid-cols-3 gap-3">
        {component.metrics.map((metric, index) => <StatTile key={`${metric.label}-${index}`} metric={metric} />)}
      </dl>
    </section>
  );
}

const sentimentClasses: Record<Metric['sentiment'], string> = {
  positive: 'border-success/30 bg-success-bg',
  negative: 'border-evidence-weak/30 bg-evidence-weak-bg',
  neutral: 'border-border-subtle bg-surface-elevated',
};

function StatTile({ metric }: { metric: Metric }) {
  return (
    <div className={`rounded-lg border p-3 ${sentimentClasses[metric.sentiment]}`}>
      <dt className="text-[11px] text-text-tertiary">{metric.label}</dt>
      <dd className="mt-0.5">
        <span className="text-base font-semibold text-text-primary">
          {metric.value}
          {metric.unit && <span className="text-xs font-normal text-text-secondary"> {metric.unit}</span>}
        </span>
        {metric.trend && (
          <span className="ml-2 text-xs text-text-secondary" data-trend={metric.trend}>
            <span aria-hidden="true">{trendArrow(metric.trend)}</span>
            <span className="ml-0.5">{trendLabel(metric.trend)}</span>
          </span>
        )}
      </dd>
      {metric.context && <p className="mt-1 text-[11px] text-text-tertiary">{metric.context}</p>}
      {/* ADR: Metric has no field distinguishing an estimate from an unverified
          number, so every verified=false metric gets the single "Unverified" tag. */}
      {!metric.verified && <span className="mt-1 inline-block text-[10px] text-evidence-moderate">Unverified</span>}
    </div>
  );
}

function trendArrow(trend: Metric['trend']): string {
  switch (trend) {
    case 'up': return '↑';
    case 'down': return '↓';
    case 'flat': return '→';
    case null: return '';
    default: return assertNever(trend);
  }
}

function trendLabel(trend: Metric['trend']): string {
  switch (trend) {
    case 'up': return 'Up';
    case 'down': return 'Down';
    case 'flat': return 'Flat';
    case null: return '';
    default: return assertNever(trend);
  }
}
