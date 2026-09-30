import type { CalloutComponent } from '@/lib/cei-ui/schema';
import { SafeMarkdown } from './markdown';
import { assertNever } from './util';

export function CalloutView({ component }: { component: CalloutComponent }) {
  return (
    <aside className="cei-callout" role="note" data-tone={component.tone}>
      {/* ADR: tone label text, not color alone | accessibility requirement in plan */}
      <span className="cei-callout-tone-label">{toneLabel(component.tone)}</span>
      {component.title && <h3 className="cei-callout-title">{component.title}</h3>}
      <SafeMarkdown content={component.content} className="cei-callout-content" />
    </aside>
  );
}

function toneLabel(tone: CalloutComponent['tone']): string {
  switch (tone) {
    case 'info': return 'Info';
    case 'success': return 'Success';
    case 'warning': return 'Warning';
    case 'danger': return 'Danger';
    case 'neutral': return 'Note';
    default: return assertNever(tone);
  }
}
