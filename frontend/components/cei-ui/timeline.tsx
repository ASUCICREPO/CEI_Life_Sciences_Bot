import type { TimelineComponent, TimelineItem } from '@/lib/cei-ui/schema';
import { assertNever } from './util';

export function TimelineView({ component }: { component: TimelineComponent }) {
  return (
    <section className="cei-timeline" aria-label={component.title ?? 'Timeline'}>
      {component.title && <h3 className="cei-timeline-title">{component.title}</h3>}
      <ol>
        {component.items.map((item) => <TimelineEntry key={item.item_id} item={item} />)}
      </ol>
    </section>
  );
}

function TimelineEntry({ item }: { item: TimelineItem }) {
  return (
    <li data-status={item.status ?? undefined}>
      <span className="cei-timeline-date">{item.date_label}</span>
      <strong className="cei-timeline-item-title">{item.title}</strong>
      {item.status && <span className="cei-timeline-status">{statusLabel(item.status)}</span>}
      {item.description && <p>{item.description}</p>}
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
