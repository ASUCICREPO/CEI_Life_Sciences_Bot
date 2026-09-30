'use client';

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
import { dispatchCEIUIAction } from '@/lib/cei-ui/actions';
import {
  MAX_COMPONENT_DEPTH,
  type ActionDescriptor,
  type ActionResponse,
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
} from '@/lib/cei-ui/schema';
import { SafeMarkdown } from '@/components/cei-ui/markdown';
import { TableView } from '@/components/cei-ui/table';
import { StatGridView } from '@/components/cei-ui/stat-grid';
import { CalloutView } from '@/components/cei-ui/callout';
import { TimelineView } from '@/components/cei-ui/timeline';
import { ComparisonView } from '@/components/cei-ui/comparison';
import { ArtifactView } from '@/components/cei-ui/artifact-frame';
import { assertNever } from '@/components/cei-ui/util';

type CEIUISurfaceProps = {
  surface: CEIUISurface;
  sessionId: string;
  fallbackText?: string;
  onAction?: (response: ActionResponse) => void;
};

type RenderContext = {
  surface: CEIUISurface;
  componentsById: Map<string, CEIUIComponent>;
  sessionId: string;
  onAction?: (response: ActionResponse) => void;
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

export function CEIUISurface({ surface, sessionId, fallbackText, onAction }: CEIUISurfaceProps) {
  const resetToken = `${surface.surface_id}:${surface.revision}`;
  return (
    <CEIUIErrorBoundary fallbackText={fallbackText} resetToken={resetToken}>
      <SurfaceTree key={resetToken} surface={surface} sessionId={sessionId} onAction={onAction} />
    </CEIUIErrorBoundary>
  );
}

function SurfaceTree({ surface, sessionId, onAction }: Omit<CEIUISurfaceProps, 'fallbackText'>) {
  const componentsById = new Map(surface.components.map((component) => [component.id, component]));
  const context = {} as RenderContext;
  context.surface = surface;
  context.componentsById = componentsById;
  context.sessionId = sessionId;
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
    <section className="cei-ui-surface" aria-label="Agent generated interface">
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
        <figure className="cei-image-fallback">
          <a href={component.src} target="_blank" rel="noopener noreferrer">
            <span aria-hidden="true">Image</span>
            <strong>{component.alt || 'Open source image'}</strong>
          </a>
          {component.width && component.height && <figcaption>{component.width} x {component.height}</figcaption>}
        </figure>
      );
    case 'Card':
      return (
        <ActionContainer
          className="cei-card"
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
      return <ListTag className="cei-list">{component.children.map((id) => <li key={id}>{context.renderId(id, ancestors)}</li>)}</ListTag>;
    }
    case 'Row':
      return <div className="cei-row">{children(component.children)}</div>;
    case 'Column':
      return <div className="cei-column">{children(component.children)}</div>;
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
      return <span className="cei-confidence" title={`Confidence ${Math.round(component.score * 100)} percent`}><strong>{component.label}</strong> {Math.round(component.score * 100)}%</span>;
    case 'EvidencePanel':
      return (
        <section className="cei-evidence" aria-label="Evidence">
          <p className="cei-evidence-summary">{component.summary}</p>
          {component.citations.length > 0 && <div className="cei-citations">{component.citations.map((citation) => <CitationLink key={citation.citation_id} citation={citation} />)}</div>}
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
    case 'h1': return <h1 className="cei-text cei-h1">{component.content}</h1>;
    case 'h2': return <h2 className="cei-text cei-h2">{component.content}</h2>;
    case 'h3': return <h3 className="cei-text cei-h3">{component.content}</h3>;
    case 'h4': return <h4 className="cei-text cei-h4">{component.content}</h4>;
    case 'h5': return <h5 className="cei-text cei-h5">{component.content}</h5>;
    case 'caption': return <p className="cei-text cei-caption">{component.content}</p>;
    case 'body': return <p className="cei-text cei-body">{component.content}</p>;
    default: return assertNever(component.variant);
  }
}

function ChoicePickerView({ component, context }: { component: ChoicePickerComponent; context: RenderContext }) {
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [selectedIndexes, setSelectedIndexes] = useState<Set<number>>(new Set());
  const action = useComponentAction(component.id, context);

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
    <fieldset className="cei-control-group" disabled={action.state.status === 'pending'}>
      <legend>{component.label}</legend>
      {component.multi ? component.options.map((option, index) => (
        <label className="cei-choice" key={`${typeof option.value}:${String(option.value)}`}>
          <input type="checkbox" checked={selectedIndexes.has(index)} onChange={() => toggle(index)} />
          <span><strong>{option.label}</strong>{option.description && <small>{option.description}</small>}</span>
        </label>
      )) : (
        <select value={selectedIndex} onChange={(event) => setSelectedIndex(Number(event.target.value))} aria-label={component.label}>
          {component.options.map((option, index) => <option key={`${typeof option.value}:${String(option.value)}`} value={index}>{option.label}</option>)}
        </select>
      )}
      <button type="button" onClick={() => action.run(component.interaction_id, component.action, selectedValue)}>{component.action.label}</button>
      <ActionFeedback state={action.state} />
    </fieldset>
  );
}

function TextFieldView({ component, context }: { component: TextFieldComponent; context: RenderContext }) {
  const [value, setValue] = useState('');
  const action = useComponentAction(component.id, context);
  const fieldId = `cei-field-${component.id}`;
  return (
    <div className="cei-control-group">
      <label htmlFor={fieldId}>{component.label}</label>
      {component.multiline
        ? <textarea id={fieldId} value={value} placeholder={component.placeholder} onChange={(event) => setValue(event.target.value)} disabled={action.state.status === 'pending'} />
        : <input id={fieldId} type="text" value={value} placeholder={component.placeholder} onChange={(event) => setValue(event.target.value)} disabled={action.state.status === 'pending'} />}
      <button type="button" disabled={action.state.status === 'pending'} onClick={() => action.run(component.interaction_id, component.action, value)}>{component.action.label}</button>
      <ActionFeedback state={action.state} />
    </div>
  );
}

function ButtonView({ component, context }: { component: Extract<CEIUIComponent, { type: 'Button' }>; context: RenderContext }) {
  const action = useComponentAction(component.id, context);
  return (
    <div className="cei-action-block">
      <button type="button" disabled={action.state.status === 'pending'} onClick={() => action.run(component.interaction_id, component.action, null)}>{component.label}</button>
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
  return (
    <article className={className}>
      {title && <h2>{title}</h2>}
      {body && <p>{body}</p>}
      {children}
      {interactionId && actions.length > 0 && <div className="cei-actions">{actions.map((descriptor) => (
        <button key={descriptor.action_id} type="button" disabled={action.state.status === 'pending'} onClick={() => action.run(interactionId, descriptor, null)}>{descriptor.label}</button>
      ))}</div>}
      <ActionFeedback state={action.state} />
    </article>
  );
}

function TabsView({ component, context, ancestors }: { component: TabsComponent; context: RenderContext; ancestors: ReadonlySet<string> }) {
  const [activeIndex, setActiveIndex] = useState(component.active_index);
  return (
    <div className="cei-tabs">
      <div role="tablist" aria-label="Sections" className="cei-tab-list">
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
          >
            {tab.label}
          </button>
        ))}
      </div>
      <div id={`${component.id}-panel-${activeIndex}`} role="tabpanel" aria-labelledby={`${component.id}-tab-${activeIndex}`} className="cei-tab-panel">
        {context.renderId(component.tabs[activeIndex].child_id, ancestors)}
      </div>
    </div>
  );
}

function ModalView({ component, context, children }: { component: ModalComponent; context: RenderContext; children: ReactNode }) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const action = useComponentAction(component.id, context);
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
    <div className="cei-modal-backdrop">
      <div ref={dialogRef} className="cei-modal" role="dialog" aria-modal="true" aria-labelledby={`${component.id}-title`} tabIndex={-1} onKeyDown={handleKeyDown}>
        <h2 id={`${component.id}-title`}>{component.title}</h2>
        {component.body && <p>{component.body}</p>}
        {children}
        {component.interaction_id && component.actions.length > 0 && <div className="cei-actions">{component.actions.map((descriptor) => (
          <button key={descriptor.action_id} type="button" disabled={action.state.status === 'pending'} onClick={() => action.run(component.interaction_id as string, descriptor, null)}>{descriptor.label}</button>
        ))}</div>}
        <ActionFeedback state={action.state} />
      </div>
    </div>
  );
}

function GraphView({ component }: { component: GraphComponent }) {
  const dataLength = Math.max(component.categories.length, ...component.series.map((series) => series.values.length));
  if (dataLength === 0) return <figure className="cei-graph"><figcaption>{component.title || 'Graph'}</figcaption><p>No graph data is available.</p></figure>;
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
        <figure className="cei-graph">
          {component.title && <figcaption>{component.title}</figcaption>}
          <div className="cei-pie-grid">{component.series.map((series, seriesIndex) => (
            <div className="cei-pie-series" key={`${series.name}-${seriesIndex}`}>
              {component.series.length > 1 && <h3>{series.name}</h3>}
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
  return <figure className="cei-graph">{component.title && <figcaption>{component.title}</figcaption>}<div className="cei-chart"><ResponsiveContainer width="100%" height="100%">{chart}</ResponsiveContainer></div></figure>;
}

function CitationLink({ citation, compact = false }: { citation: CitationDescriptor; compact?: boolean }) {
  return (
    <a className={compact ? 'cei-citation-chip' : 'cei-citation'} href={citation.url} target="_blank" rel="noopener noreferrer">
      <span>{citation.source_type}{citation.year !== null ? `, ${citation.year}` : ''}</span>
      <strong>{citation.title}</strong>
      {!compact && citation.relevant_passage && <small>{citation.relevant_passage}</small>}
    </a>
  );
}

function ReviewGateView({ component, context }: { component: ReviewGateComponent; context: RenderContext }) {
  const action = useComponentAction(component.id, context);
  return (
    <section className="cei-review" aria-label="Plan review">
      <header><span>{component.require_approval ? 'Approval required' : 'Review'}</span><h2>{component.plan.summary}</h2></header>
      <ol>{component.plan.steps.map((step) => <li key={step.step_id} data-status={step.status}><strong>{step.title}</strong>{step.description && <p>{step.description}</p>}<span>{step.status.replace('_', ' ')}</span></li>)}</ol>
      <div className="cei-actions">{component.actions.map((descriptor) => <button key={descriptor.action_id} type="button" disabled={action.state.status === 'pending'} onClick={() => action.run(component.interaction_id, descriptor, null)}>{descriptor.label}</button>)}</div>
      <ActionFeedback state={action.state} />
    </section>
  );
}

function useComponentAction(componentId: string, context: RenderContext) {
  const [state, setState] = useState<ActionState>({ status: 'idle', message: '' });
  async function run(interactionId: string, descriptor: ActionDescriptor, value: JsonValue) {
    setState({ status: 'pending', message: 'Sending action.' });
    try {
      const response = await dispatchCEIUIAction({
        session_id: context.sessionId,
        surface_id: context.surface.surface_id,
        revision: context.surface.revision,
        interaction_id: interactionId,
        component_id: componentId,
        action_id: descriptor.action_id,
        value,
      });
      context.onAction?.(response);
      const succeeded = response.status === 'accepted' || response.status === 'completed';
      setState({
        status: succeeded ? 'success' : 'error',
        message: response.message ?? (succeeded ? 'Action received.' : `Action ${response.status}.`),
      });
    } catch (error) {
      setState({ status: 'error', message: error instanceof Error ? error.message : 'The action could not be completed.' });
    }
  }
  return { state, run };
}

function ActionFeedback({ state }: { state: ActionState }) {
  if (state.status === 'idle') return null;
  return <p className={`cei-action-status ${state.status}`} role={state.status === 'error' ? 'alert' : 'status'}>{state.message}</p>;
}

function CEIUIFallback({ text, error }: { text?: string; error: string }) {
  return <div className="cei-ui-fallback"><p className="preview-error" role="status">{error}</p>{text && <p className="response-text">{text}</p>}</div>;
}

class CEIUIErrorBoundary extends Component<BoundaryProps, BoundaryState> {
  state: BoundaryState = { hasError: false };
  static getDerivedStateFromError(): BoundaryState { return { hasError: true }; }
  componentDidUpdate(previousProps: BoundaryProps) {
    if (this.state.hasError && previousProps.resetToken !== this.props.resetToken) this.setState({ hasError: false });
  }
  render() {
    if (this.state.hasError) return <CEIUIFallback text={this.props.fallbackText} error="The agent returned a CEI UI surface that this preview cannot render." />;
    return this.props.children;
  }
}

function getFocusable(container: HTMLElement | null): HTMLElement[] {
  if (!container) return [];
  return [...container.querySelectorAll<HTMLElement>('a[href], button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])')];
}

