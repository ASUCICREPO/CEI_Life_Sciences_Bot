import type { Metric, StatGridComponent } from '@/lib/cei-ui/schema';
import { assertNever } from './util';

export function StatGridView({ component }: { component: StatGridComponent }) {
  return (
    <section className="cei-statgrid" aria-label={component.title ?? 'Metrics'}>
      {component.title && <h3 className="cei-statgrid-title">{component.title}</h3>}
      <dl className="cei-statgrid-grid">
        {component.metrics.map((metric, index) => <StatTile key={`${metric.label}-${index}`} metric={metric} />)}
      </dl>
    </section>
  );
}

function StatTile({ metric }: { metric: Metric }) {
  return (
    <div className="cei-statgrid-tile" data-sentiment={metric.sentiment}>
      <dt>{metric.label}</dt>
      <dd>
        <span className="cei-statgrid-value">
          {metric.value}
          {metric.unit && <span className="cei-statgrid-unit"> {metric.unit}</span>}
        </span>
        {metric.trend && (
          <span className="cei-statgrid-trend" data-trend={metric.trend}>
            <span aria-hidden="true">{trendArrow(metric.trend)}</span>
            <span>{trendLabel(metric.trend)}</span>
          </span>
        )}
      </dd>
      {metric.context && <p className="cei-statgrid-context">{metric.context}</p>}
      {/* ADR: Metric has no field distinguishing an estimate from an unverified
          number, so every verified=false metric gets the single "Unverified" tag. */}
      {!metric.verified && <span className="cei-statgrid-tag">Unverified</span>}
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
