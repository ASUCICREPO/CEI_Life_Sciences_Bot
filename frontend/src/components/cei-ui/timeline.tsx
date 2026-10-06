import type { TimelineComponent, TimelineItem } from '../../lib/cei-ui/schema';
import { assertNever } from './util';

export function TimelineView({ component }: { component: TimelineComponent }) {
  return (
    <section className="my-3" aria-label={component.title ?? 'Timeline'}>
      {component.title && <h3 className="text-sm font-semibold text-text-primary mb-2">{component.title}</h3>}
      <ol className="space-y-3 border-l border-border-subtle pl-4">
        {component.items.map((item) => <TimelineEntry key={item.item_id} item={item} />)}
      </ol>
    </section>
  );
}

function TimelineEntry({ item }: { item: TimelineItem }) {
  return (
    <li className="relative">
      <span className="absolute -left-[21px] top-1 h-2 w-2 rounded-full bg-cei-blue-light" aria-hidden="true" />
      <span className="text-[11px] text-text-tertiary">{item.date_label}</span>
      <strong className="block text-sm text-text-primary">{item.title}</strong>
      {item.status && <span className="text-[11px] text-text-secondary">{statusLabel(item.status)}</span>}
      {item.description && <p className="text-xs text-text-secondary mt-0.5">{item.description}</p>}
    </li>
  );
}

function statusLabel(status: NonNullable<TimelineItem['status']>): string {
  switch (status) {
    case 'completed': return 'Completed';
    case 'in_progress': return 'In progress';
    case 'planned': return 'Planned';
    case 'blocked': return 'Blocked';
    default: return assertNever(status);
  }
}
