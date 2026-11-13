import type { CalloutComponent } from '../../lib/cei-ui/schema';
import { SafeMarkdown } from './markdown';
import { assertNever } from './util';

const toneClasses: Record<CalloutComponent['tone'], string> = {
  info: 'border-cei-blue/20 bg-cei-blue/5 text-cei-blue',
  success: 'border-success/30 bg-success-bg text-success',
  warning: 'border-evidence-moderate/30 bg-evidence-moderate-bg text-evidence-moderate',
  danger: 'border-evidence-weak/30 bg-evidence-weak-bg text-evidence-weak',
  neutral: 'border-border-subtle bg-surface-panel text-text-secondary',
};

export function CalloutView({ component }: { component: CalloutComponent }) {
  return (
    <aside className={`my-3 rounded-lg border p-3 ${toneClasses[component.tone]}`} role="note">
      {/* ADR: tone label text, not color alone | accessibility requirement in plan */}
      <span className="text-[10px] font-semibold uppercase tracking-wide">{toneLabel(component.tone)}</span>
      {component.title && <h3 className="text-sm font-semibold mt-0.5">{component.title}</h3>}
      <SafeMarkdown content={component.content} />
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
