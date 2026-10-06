import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import {
  Component,
  useEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from 'react';
import { dispatchCEIUIAction } from '../../lib/cei-ui/actions';
import {
  MAX_COMPONENT_DEPTH,
  type ActionDescriptor,
  type CEIUIComponent,
  type CEIUISurface,
  type ChoicePickerComponent,
  type CitationDescriptor,
  type GraphComponent,
  type JsonValue,
  type ModalComponent,
  type ReviewGateComponent,
  type TabsComponent,
  type TextFieldComponent,
} from '../../lib/cei-ui/schema';
import type { AgentEnvelope, AgentName } from '../../lib/agentClient';
import { CitationChip, toChipSourceType } from '../../design-system/CitationChip';
import { ConfidenceBadge } from '../../design-system/ConfidenceBadge';
import type { EvidenceStrengthType } from '../../design-system/tokens';
import { SafeMarkdown } from './markdown';
import { TableView } from './table';
import { StatGridView } from './stat-grid';
import { CalloutView } from './callout';
import { TimelineView } from './timeline';
import { ComparisonView } from './comparison';
import { ArtifactView } from './artifact-frame';
import { assertNever } from './util';

type CEIUISurfaceProps = {
  surface: CEIUISurface;
  agent: AgentName;
  sessionId: string;
  fallbackText?: string;
  onAction?: (response: AgentEnvelope) => void;
  disabled?: boolean;
};

type RenderContext = {
  surface: CEIUISurface;
  agent: AgentName;
  componentsById: Map<string, CEIUIComponent>;
  sessionId: string;
  disabled: boolean;
  onAction?: (response: AgentEnvelope) => void;
  renderId: (componentId: string, ancestors?: ReadonlySet<string>) => ReactNode;
};

type BoundaryProps = {
  children: ReactNode;
  fallbackText?: string;
  resetToken: string;
};

type BoundaryState = { hasError: boolean };
type ActionState = { status: 'idle' | 'pending' | 'success' | 'error'; message: string };

const GRAPH_COLORS = ['#16625a', '#b7791f', '#2d5f9a', '#a43f35', '#6b5b95', '#39736d'];

// ADR: reuse the design-system evidence-strength scale for CEI UI confidence |
// 0-1 score has no server-side strength label, so bucket it the same way the
// rest of the dashboard buckets confidence.
function scoreToEvidenceStrength(score: number): EvidenceStrengthType {
  if (score >= 0.8) return 'strong';
  if (score >= 0.5) return 'moderate';
  return 'weak';
}

export function CEIUISurface({ surface, agent, sessionId, fallbackText, onAction, disabled }: CEIUISurfaceProps) {
  const resetToken = `${surface.surface_id}:${surface.revision}`;
  return (
    <CEIUIErrorBoundary fallbackText={fallbackText} resetToken={resetToken}>
      <SurfaceTree key={resetToken} surface={surface} agent={agent} sessionId={sessionId} onAction={onAction} disabled={disabled ?? false} />
    </CEIUIErrorBoundary>
  );
}

function SurfaceTree({ surface, agent, sessionId, onAction, disabled }: Omit<CEIUISurfaceProps, 'fallbackText'>) {
  const componentsById = new Map(surface.components.map((component) => [component.id, component]));
  const context = {} as RenderContext;
  context.surface = surface;
  context.agent = agent;
  context.componentsById = componentsById;
  context.sessionId = sessionId;
  context.disabled = disabled ?? false;
  context.onAction = onAction;
  context.renderId = (componentId, ancestors = new Set()) => {
    if (ancestors.has(componentId) || ancestors.size >= MAX_COMPONENT_DEPTH) {
      return <CEIUIFallback error="The CEI UI component graph cannot be rendered safely." />;
    }
    const component = componentsById.get(componentId);
    if (!component) return <CEIUIFallback error="The CEI UI surface references a missing component." />;
    const nextAncestors = new Set(ancestors);
    nextAncestors.add(componentId);
    return <ComponentRenderer key={componentId} component={component} context={context} ancestors={nextAncestors} />;
  };

  return (
    <section className="cei-ui-surface space-y-3" aria-label="Agent generated interface">
      {context.renderId(surface.root_id)}
    </section>
  );
}

function ComponentRenderer({
  component,
  context,
  ancestors,
}: {
  component: CEIUIComponent;
  context: RenderContext;
  ancestors: ReadonlySet<string>;
}) {
  const children = (ids: string[]) => ids.map((id) => context.renderId(id, ancestors));

  switch (component.type) {
    case 'Text':
      return <TextView component={component} />;
    case 'Image':
      return (
        <figure className="my-2">
          <a className="inline-flex items-center gap-2 text-sm text-cei-blue underline" href={component.src} target="_blank" rel="noopener noreferrer">
            <span aria-hidden="true">Image</span>
            <strong>{component.alt || 'Open source image'}</strong>
          </a>
          {component.width && component.height && <figcaption className="text-xs text-text-tertiary">{component.width} x {component.height}</figcaption>}
        </figure>
      );
    case 'Card':
      return (
        <ActionContainer
          className="rounded-lg border border-border-subtle bg-surface-elevated p-4"
          title={component.title}
          body={component.body}
          componentId={component.id}
          interactionId={component.interaction_id}
          actions={component.actions}
          context={context}
        >
          {children(component.children)}
        </ActionContainer>
      );
    case 'ChoicePicker':
      return <ChoicePickerView component={component} context={context} />;
    case 'TextField':
      return <TextFieldView component={component} context={context} />;
    case 'Button':
      return <ButtonView component={component} context={context} />;
    case 'List': {
      const ListTag = component.ordered ? 'ol' : 'ul';
      return <ListTag className={`text-sm text-text-primary space-y-1 pl-5 ${component.ordered ? 'list-decimal' : 'list-disc'}`}>{component.children.map((id) => <li key={id}>{context.renderId(id, ancestors)}</li>)}</ListTag>;
    }
    case 'Row':
      return <div className="flex flex-wrap items-start gap-3">{children(component.children)}</div>;
    case 'Column':
      return <div className="flex flex-col gap-3">{children(component.children)}</div>;
    case 'Tabs':
      return <TabsView component={component} context={context} ancestors={ancestors} />;
    case 'Modal':
      return <ModalView component={component} context={context}>{children(component.children)}</ModalView>;
    case 'Markdown':
      return <SafeMarkdown content={component.content} />;
    case 'Graph':
      return <GraphView component={component} />;
    case 'Table':
      return <TableView component={component} />;
    case 'StatGrid':
      return <StatGridView component={component} />;
    case 'Callout':
      return <CalloutView component={component} />;
    case 'Timeline':
      return <TimelineView component={component} />;
    case 'Comparison':
      return <ComparisonView component={component} />;
    case 'Artifact':
      return <ArtifactView component={component} />;
    case 'CitationChip':
      return <CitationLink citation={component.citation} compact />;
    case 'ConfidenceBadge':
      return (
        <span className="inline-flex items-center gap-1.5" title={`Confidence ${Math.round(component.score * 100)} percent`}>
          <strong className="text-xs font-medium text-text-secondary">{component.label}</strong>
          <ConfidenceBadge level={scoreToEvidenceStrength(component.score)} showLabel={false} size="sm" />
        </span>
      );
    case 'EvidencePanel':
      return (
        <section className="rounded-lg border border-border-subtle bg-surface-panel p-3" aria-label="Evidence">
          <p className="text-xs text-text-secondary">{component.summary}</p>
          {component.citations.length > 0 && <div className="mt-2 flex flex-wrap gap-1.5">{component.citations.map((citation) => <CitationLink key={citation.citation_id} citation={citation} />)}</div>}
          {children(component.children)}
        </section>
      );
    case 'ReviewGate':
      return <ReviewGateView component={component} context={context} />;
    default:
      return assertNever(component);
  }
}

function TextView({ component }: { component: Extract<CEIUIComponent, { type: 'Text' }> }) {
  switch (component.variant) {
    case 'h1': return <h1 className="text-xl font-semibold text-text-primary">{component.content}</h1>;
    case 'h2': return <h2 className="text-lg font-semibold text-text-primary">{component.content}</h2>;
    case 'h3': return <h3 className="text-base font-semibold text-text-primary">{component.content}</h3>;
    case 'h4': return <h4 className="text-sm font-semibold text-text-primary">{component.content}</h4>;
    case 'h5': return <h5 className="text-sm font-medium text-text-primary">{component.content}</h5>;
    case 'caption': return <p className="text-xs text-text-tertiary">{component.content}</p>;
    case 'body': return <p className="text-sm text-text-primary leading-relaxed">{component.content}</p>;
    default: return assertNever(component.variant);
  }
}

function ChoicePickerView({ component, context }: { component: ChoicePickerComponent; context: RenderContext }) {
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [selectedIndexes, setSelectedIndexes] = useState<Set<number>>(new Set());
  const action = useComponentAction(component.id, context);
  const disabled = context.disabled || action.state.status === 'pending';

  function toggle(index: number) {
    setSelectedIndexes((current) => {
      const next = new Set(current);
      if (next.has(index)) next.delete(index); else next.add(index);
      return next;
    });
  }

  const selectedValue: JsonValue = component.multi
    ? [...selectedIndexes].sort((left, right) => left - right).map((index) => component.options[index].value)
    : component.options[selectedIndex].value;

  return (
    <fieldset className="rounded-lg border border-border-subtle bg-surface-elevated p-3 space-y-2" disabled={disabled}>
      <legend className="text-sm font-medium text-text-primary">{component.label}</legend>
      {component.multi ? component.options.map((option, index) => (
        <label className="flex items-center gap-2 text-sm text-text-primary" key={`${typeof option.value}:${String(option.value)}`}>
          <input type="checkbox" checked={selectedIndexes.has(index)} onChange={() => toggle(index)} />
          <span><strong>{option.label}</strong>{option.description && <small className="ml-1 text-text-tertiary">{option.description}</small>}</span>
        </label>
      )) : (
        <select className="w-full rounded-md border border-border-subtle px-2 py-1.5 text-sm" value={selectedIndex} onChange={(event) => setSelectedIndex(Number(event.target.value))} aria-label={component.label}>
          {component.options.map((option, index) => <option key={`${typeof option.value}:${String(option.value)}`} value={index}>{option.label}</option>)}
        </select>
      )}
      <button type="button" className="rounded-md bg-cei-blue px-3 py-1.5 text-xs font-medium text-white disabled:opacity-40" disabled={disabled} onClick={() => action.run(component.interaction_id, component.action, selectedValue)}>{component.action.label}</button>
      <ActionFeedback state={action.state} />
    </fieldset>
  );
}

function TextFieldView({ component, context }: { component: TextFieldComponent; context: RenderContext }) {
  const [value, setValue] = useState('');
  const action = useComponentAction(component.id, context);
  const disabled = context.disabled || action.state.status === 'pending';
  const fieldId = `cei-field-${component.id}`;
  return (
    <div className="rounded-lg border border-border-subtle bg-surface-elevated p-3 space-y-2">
      <label className="text-sm font-medium text-text-primary" htmlFor={fieldId}>{component.label}</label>
      {component.multiline
        ? <textarea id={fieldId} value={value} placeholder={component.placeholder} onChange={(event) => setValue(event.target.value)} disabled={disabled} className="w-full rounded-md border border-border-subtle px-2 py-1.5 text-sm" rows={3} />
        : <input id={fieldId} type="text" value={value} placeholder={component.placeholder} onChange={(event) => setValue(event.target.value)} disabled={disabled} className="w-full rounded-md border border-border-subtle px-2 py-1.5 text-sm" />}
      <button type="button" className="rounded-md bg-cei-blue px-3 py-1.5 text-xs font-medium text-white disabled:opacity-40" disabled={disabled} onClick={() => action.run(component.interaction_id, component.action, value)}>{component.action.label}</button>
      <ActionFeedback state={action.state} />
    </div>
  );
}

function ButtonView({ component, context }: { component: Extract<CEIUIComponent, { type: 'Button' }>; context: RenderContext }) {
  const action = useComponentAction(component.id, context);
  const disabled = context.disabled || action.state.status === 'pending';
  return (
    <div className="my-1">
      <button type="button" className="rounded-md bg-cei-blue px-3 py-1.5 text-xs font-medium text-white disabled:opacity-40" disabled={disabled} onClick={() => action.run(component.interaction_id, component.action, null)}>{component.label}</button>
      <ActionFeedback state={action.state} />
    </div>
  );
}

function ActionContainer({
  className,
  title,
  body,
  componentId,
  interactionId,
  actions,
  context,
  children,
}: {
  className: string;
  title: string | null;
  body: string | null;
  componentId: string;
  interactionId: string | null;
  actions: ActionDescriptor[];
  context: RenderContext;
  children: ReactNode;
}) {
  const action = useComponentAction(componentId, context);
  const disabled = context.disabled || action.state.status === 'pending';
  return (
    <article className={className}>
      {title && <h2 className="text-sm font-semibold text-text-primary mb-1">{title}</h2>}
      {body && <p className="text-sm text-text-secondary mb-2">{body}</p>}
      {children}
      {interactionId && actions.length > 0 && <div className="mt-3 flex flex-wrap gap-2">{actions.map((descriptor) => (
        <button key={descriptor.action_id} type="button" className="rounded-md bg-cei-blue px-3 py-1.5 text-xs font-medium text-white disabled:opacity-40" disabled={disabled} onClick={() => action.run(interactionId, descriptor, null)}>{descriptor.label}</button>
      ))}</div>}
      <ActionFeedback state={action.state} />
    </article>
  );
}

function TabsView({ component, context, ancestors }: { component: TabsComponent; context: RenderContext; ancestors: ReadonlySet<string> }) {
  const [activeIndex, setActiveIndex] = useState(component.active_index);
  return (
    <div>
      <div role="tablist" aria-label="Sections" className="flex flex-wrap gap-1 border-b border-border-subtle">
        {component.tabs.map((tab, index) => (
          <button
            id={`${component.id}-tab-${index}`}
            key={tab.child_id}
            type="button"
            role="tab"
            aria-selected={activeIndex === index}
            aria-controls={`${component.id}-panel-${index}`}
            tabIndex={activeIndex === index ? 0 : -1}
            onClick={() => setActiveIndex(index)}
            className={`px-3 py-2 text-xs font-medium border-b-2 -mb-px ${activeIndex === index ? 'border-cei-blue text-cei-blue' : 'border-transparent text-text-secondary hover:text-text-primary'}`}
          >
            {tab.label}
          </button>
        ))}
      </div>
      <div id={`${component.id}-panel-${activeIndex}`} role="tabpanel" aria-labelledby={`${component.id}-tab-${activeIndex}`} className="pt-3">
        {context.renderId(component.tabs[activeIndex].child_id, ancestors)}
      </div>
    </div>
  );
}

function ModalView({ component, context, children }: { component: ModalComponent; context: RenderContext; children: ReactNode }) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const action = useComponentAction(component.id, context);
  const disabled = context.disabled || action.state.status === 'pending';
  useEffect(() => {
    if (!component.open) return;
    const firstFocusable = getFocusable(dialogRef.current)[0];
    (firstFocusable ?? dialogRef.current)?.focus();
  }, [component.open]);
  if (!component.open) return null;

  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === 'Escape' && component.interaction_id) {
      const dismissAction = component.actions.find((descriptor) => descriptor.intent === 'dismiss');
      if (dismissAction) void action.run(component.interaction_id, dismissAction, null);
    }
    if (event.key !== 'Tab') return;
    const focusable = getFocusable(dialogRef.current);
    if (focusable.length === 0) {
      event.preventDefault();
      dialogRef.current?.focus();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div ref={dialogRef} className="w-full max-w-md rounded-xl bg-surface-elevated p-5 shadow-xl" role="dialog" aria-modal="true" aria-labelledby={`${component.id}-title`} tabIndex={-1} onKeyDown={handleKeyDown}>
        <h2 id={`${component.id}-title`} className="text-base font-semibold text-text-primary">{component.title}</h2>
        {component.body && <p className="text-sm text-text-secondary mt-1">{component.body}</p>}
        {children}
        {component.interaction_id && component.actions.length > 0 && <div className="mt-4 flex flex-wrap gap-2">{component.actions.map((descriptor) => (
          <button key={descriptor.action_id} type="button" className="rounded-md bg-cei-blue px-3 py-1.5 text-xs font-medium text-white disabled:opacity-40" disabled={disabled} onClick={() => action.run(component.interaction_id as string, descriptor, null)}>{descriptor.label}</button>
        ))}</div>}
        <ActionFeedback state={action.state} />
      </div>
    </div>
  );
}

function GraphView({ component }: { component: GraphComponent }) {
  const dataLength = Math.max(component.categories.length, ...component.series.map((series) => series.values.length));
  if (dataLength === 0) return <figure className="my-3"><figcaption className="text-sm font-semibold text-text-primary">{component.title || 'Graph'}</figcaption><p className="text-xs text-text-tertiary">No graph data is available.</p></figure>;
  const data = Array.from({ length: dataLength }, (_, index) => ({
    category: component.categories[index] ?? index + 1,
    ...Object.fromEntries(component.series.map((series, seriesIndex) => [`series_${seriesIndex}`, series.values[index] ?? null])),
  }));
  const common = <><CartesianGrid strokeDasharray="3 3" /><Tooltip /><Legend /></>;
  const xAxis = <XAxis dataKey="category" label={component.x_label ? { value: component.x_label, position: 'insideBottom', offset: -4 } : undefined} />;
  const yAxis = <YAxis label={component.y_label ? { value: component.y_label, angle: -90, position: 'insideLeft' } : undefined} />;

  let chart: ReactNode;
  switch (component.graph_type) {
    case 'line':
      chart = <LineChart data={data} accessibilityLayer margin={{ bottom: 24, left: 12, right: 12 }}>{common}{xAxis}{yAxis}{component.series.map((series, index) => <Line key={`${series.name}-${index}`} type="monotone" dataKey={`series_${index}`} name={series.name} stroke={GRAPH_COLORS[index % GRAPH_COLORS.length]} strokeWidth={2} connectNulls />)}</LineChart>;
      break;
    case 'bar-vertical':
      chart = <BarChart data={data} accessibilityLayer margin={{ bottom: 24, left: 12, right: 12 }}>{common}{xAxis}{yAxis}{component.series.map((series, index) => <Bar key={`${series.name}-${index}`} dataKey={`series_${index}`} name={series.name} fill={GRAPH_COLORS[index % GRAPH_COLORS.length]} />)}</BarChart>;
      break;
    case 'bar-horizontal':
      chart = <BarChart data={data} layout="vertical" accessibilityLayer margin={{ bottom: 18, left: 16, right: 20 }}>{common}<XAxis type="number" label={component.x_label ? { value: component.x_label, position: 'insideBottom', offset: -4 } : undefined} /><YAxis type="category" dataKey="category" width={96} label={component.y_label ? { value: component.y_label, angle: -90, position: 'insideLeft' } : undefined} />{component.series.map((series, index) => <Bar key={`${series.name}-${index}`} dataKey={`series_${index}`} name={series.name} fill={GRAPH_COLORS[index % GRAPH_COLORS.length]} />)}</BarChart>;
      break;
    case 'scatter':
      chart = <ScatterChart accessibilityLayer margin={{ bottom: 24, left: 12, right: 12 }}>{common}<XAxis dataKey="category" type="category" name={component.x_label ?? 'Category'} label={component.x_label ? { value: component.x_label, position: 'insideBottom', offset: -4 } : undefined} /><YAxis dataKey="value" type="number" name={component.y_label ?? 'Value'} label={component.y_label ? { value: component.y_label, angle: -90, position: 'insideLeft' } : undefined} />{component.series.map((series, index) => <Scatter key={`${series.name}-${index}`} name={series.name} data={series.values.map((value, valueIndex) => ({ category: component.categories[valueIndex] ?? valueIndex + 1, value }))} fill={GRAPH_COLORS[index % GRAPH_COLORS.length]} />)}</ScatterChart>;
      break;
    case 'pie':
      return (
        <figure className="my-3">
          {component.title && <figcaption className="text-sm font-semibold text-text-primary">{component.title}</figcaption>}
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 h-64">{component.series.map((series, seriesIndex) => (
            <div className="h-full" key={`${series.name}-${seriesIndex}`}>
              {component.series.length > 1 && <h3 className="text-xs font-medium text-text-secondary">{series.name}</h3>}
              <ResponsiveContainer width="100%" height="100%">
                <PieChart accessibilityLayer><Tooltip /><Legend /><Pie data={series.values.map((value, index) => ({ name: String(component.categories[index] ?? index + 1), value }))} dataKey="value" nameKey="name" outerRadius="72%" label>{series.values.map((_, index) => <Cell key={index} fill={GRAPH_COLORS[index % GRAPH_COLORS.length]} />)}</Pie></PieChart>
              </ResponsiveContainer>
            </div>
          ))}</div>
        </figure>
      );
    default:
      return assertNever(component.graph_type);
  }
  return <figure className="my-3">{component.title && <figcaption className="text-sm font-semibold text-text-primary">{component.title}</figcaption>}<div className="h-64"><ResponsiveContainer width="100%" height="100%">{chart}</ResponsiveContainer></div></figure>;
}

function CitationLink({ citation, compact = false }: { citation: CitationDescriptor; compact?: boolean }) {
  if (compact) {
    return (
      <CitationChip
        id={citation.citation_id}
        label={`${citation.title}${citation.year !== null ? ` (${citation.year})` : ''}`}
        sourceType={toChipSourceType(citation.source_type)}
        onClick={() => window.open(citation.url, '_blank', 'noopener,noreferrer')}
      />
    );
  }
  return (
    <a className="block rounded-lg border border-border-subtle bg-surface-elevated p-2 text-xs hover:border-cei-blue-light/40" href={citation.url} target="_blank" rel="noopener noreferrer">
      <span className="text-text-tertiary">{citation.source_type}{citation.year !== null ? `, ${citation.year}` : ''}</span>
      <strong className="block text-text-primary">{citation.title}</strong>
      {citation.relevant_passage && <small className="text-text-secondary">{citation.relevant_passage}</small>}
    </a>
  );
}

function ReviewGateView({ component, context }: { component: ReviewGateComponent; context: RenderContext }) {
  const action = useComponentAction(component.id, context);
  const disabled = context.disabled || action.state.status === 'pending';
  return (
    <section className="rounded-xl border border-review-required/30 bg-review-required-bg p-4" aria-label="Plan review">
      <header className="mb-2">
        <span className="text-[11px] font-semibold uppercase tracking-wide text-review-required">{component.require_approval ? 'Approval required' : 'Review'}</span>
        <h2 className="text-sm font-semibold text-text-primary mt-0.5">{component.plan.summary}</h2>
      </header>
      <ol className="space-y-1.5 mb-3">{component.plan.steps.map((step) => (
        <li key={step.step_id} className="flex items-start gap-2 text-xs text-text-primary">
          <span className="flex-1">
            <strong>{step.title}</strong>
            {step.description && <p className="text-text-secondary">{step.description}</p>}
          </span>
          <span className="text-[10px] text-text-tertiary uppercase">{step.status.replace('_', ' ')}</span>
        </li>
      ))}</ol>
      <div className="flex flex-wrap gap-2">{component.actions.map((descriptor) => (
        <button
          key={descriptor.action_id}
          type="button"
          className={`rounded-md px-3 py-1.5 text-xs font-medium disabled:opacity-40 ${descriptor.intent === 'reject' ? 'border border-border-subtle text-text-secondary' : 'bg-cei-blue text-white'}`}
          disabled={disabled}
          onClick={() => action.run(component.interaction_id, descriptor, null)}
        >
          {descriptor.label}
        </button>
      ))}</div>
      <ActionFeedback state={action.state} />
    </section>
  );
}

function useComponentAction(componentId: string, context: RenderContext) {
  const [state, setState] = useState<ActionState>({ status: 'idle', message: '' });
  async function run(interactionId: string, descriptor: ActionDescriptor, value: JsonValue) {
    if (context.disabled) return;
    setState({ status: 'pending', message: 'Sending action.' });
    try {
      const response = await dispatchCEIUIAction({
        agent: context.agent,
        session_id: context.sessionId,
        surface_id: context.surface.surface_id,
        revision: context.surface.revision,
        interaction_id: interactionId,
        component_id: componentId,
        action_id: descriptor.action_id,
        value,
      });
      context.onAction?.(response);
      const actionStatus = response.action_response?.status;
      const succeeded = actionStatus === 'accepted' || actionStatus === 'completed' || actionStatus === undefined;
      setState({
        status: succeeded ? 'success' : 'error',
        message: response.action_response?.message ?? (succeeded ? 'Action received.' : `Action ${actionStatus ?? 'failed'}.`),
      });
    } catch (error) {
      setState({ status: 'error', message: error instanceof Error ? error.message : 'The action could not be completed.' });
    }
  }
  return { state, run };
}

function ActionFeedback({ state }: { state: ActionState }) {
  if (state.status === 'idle') return null;
  const toneClass = state.status === 'error' ? 'text-evidence-weak' : state.status === 'pending' ? 'text-text-tertiary' : 'text-success';
  return <p className={`mt-1.5 text-xs ${toneClass}`} role={state.status === 'error' ? 'alert' : 'status'}>{state.message}</p>;
}

function CEIUIFallback({ text, error }: { text?: string; error: string }) {
  return (
    <div className="rounded-lg border border-evidence-weak/30 bg-evidence-weak-bg p-3">
      <p className="text-sm text-evidence-weak" role="status">{error}</p>
      {text && <p className="mt-1 text-sm text-text-primary">{text}</p>}
    </div>
  );
}

class CEIUIErrorBoundary extends Component<BoundaryProps, BoundaryState> {
  state: BoundaryState = { hasError: false };
  static getDerivedStateFromError(): BoundaryState { return { hasError: true }; }
  componentDidUpdate(previousProps: BoundaryProps) {
    if (this.state.hasError && previousProps.resetToken !== this.props.resetToken) this.setState({ hasError: false });
  }
  render() {
    if (this.state.hasError) return <CEIUIFallback text={this.props.fallbackText} error="The agent returned a CEI UI surface that this app cannot render." />;
    return this.props.children;
  }
}

function getFocusable(container: HTMLElement | null): HTMLElement[] {
  if (!container) return [];
  return [...container.querySelectorAll<HTMLElement>('a[href], button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])')];
}
