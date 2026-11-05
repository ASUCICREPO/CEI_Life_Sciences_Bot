import {
  CEI_UI_CONTENT_TYPE,
  CEI_UI_PROTOCOL,
  CEI_UI_VERSION,
  ComponentSchema,
  type CEIUIComponent,
  type CEIUISurface,
} from '../cei-ui/schema';
import {
  TERMINAL_EVENT_TYPES,
  parseStreamEvent,
  type GroundingStatus,
  type StreamEvent,
  type StreamEventOf,
  type StreamEventType,
  type TraceEvent,
} from './events';

// Pure reducer for the CEI UI 1.2 progressive-patch stream (.agents/specs/streaming-protocol.md).
// Dispatch is a Strategy registry — one pure `(state, event) => state` handler per event
// type — instead of a switch. The seq / terminal ordering guard runs once, before dispatch.

export type StreamStatus = 'idle' | 'streaming' | 'ended' | 'awaiting_input' | 'handed_off' | 'error';

export type GroundingState = {
  status: GroundingStatus;
  metrics?: { index: number; verified: boolean }[];
  flagged_cells?: { row: number; column: number }[];
};

export type StreamInteraction =
  | { kind: 'hitl'; seq: number; surface: CEIUISurface }
  | { kind: 'handoff'; seq: number; job_id: string; reason?: string; poll_url?: string };

export type StreamState = {
  lastSeq: number;
  status: StreamStatus;
  surface: { surface_id: string; revision: number; version: string; root_id: string } | null;
  components: ReadonlyMap<string, CEIUIComponent>;
  rootIds: readonly string[];
  traceEvents: readonly TraceEvent[];
  progress: Omit<StreamEventOf<'progress'>, 'seq' | 'type'> | null;
  usage: Omit<StreamEventOf<'usage'>, 'seq' | 'type'> | null;
  grounding: Readonly<Record<string, GroundingState>>;
  interactions: readonly StreamInteraction[];
  error?: { code: string; message: string };
  lastHeartbeatAt: string | null;
  /** Protocol violations the client tolerated (logged by the caller, never thrown). */
  warnings: readonly string[];
};

export const initialStreamState: StreamState = Object.freeze({
  lastSeq: -1,
  status: 'idle',
  surface: null,
  components: new Map(),
  rootIds: [],
  traceEvents: [],
  progress: null,
  usage: null,
  grounding: {},
  interactions: [],
  lastHeartbeatAt: null,
  warnings: [],
}) as StreamState;

type Handler<T extends StreamEventType> = (state: StreamState, event: StreamEventOf<T>) => StreamState;
type HandlerTable = { [T in StreamEventType]: Handler<T> };

const STRUCTURAL_FIELDS = new Set(['id', 'type', 'children']);

function childrenOf(component: CEIUIComponent): string[] | null {
  return 'children' in component && Array.isArray(component.children) ? component.children : null;
}

/** Drop child references that do not exist yet; children arrive via later component_add events. */
function withKnownChildren(component: CEIUIComponent, components: ReadonlyMap<string, CEIUIComponent>): CEIUIComponent {
  const children = childrenOf(component);
  if (!children) return component;
  return { ...component, children: children.filter((id) => components.has(id)) } as CEIUIComponent;
}

function replaceComponent(state: StreamState, component: CEIUIComponent): StreamState {
  const components = new Map(state.components);
  components.set(component.id, component);
  return { ...state, components };
}

function warn(state: StreamState, message: string): StreamState {
  return { ...state, warnings: [...state.warnings, message] };
}

/** Shallow-merge non-structural fields; null if the result is not a valid 1.1 component. */
function patchComponent(target: CEIUIComponent, patch: Record<string, unknown>): CEIUIComponent | null {
  const safePatch = Object.fromEntries(Object.entries(patch).filter(([key]) => !STRUCTURAL_FIELDS.has(key)));
  const parsed = ComponentSchema.safeParse({ ...target, ...safePatch });
  return parsed.success ? parsed.data : null;
}

function applyPatch(state: StreamState, componentId: string, patch: Record<string, unknown>): StreamState {
  const target = state.components.get(componentId);
  if (!target) return warn(state, `update for unknown component ${componentId}`);
  const next = patchComponent(target, patch);
  return next ? replaceComponent(state, next) : warn(state, `invalid update rejected for ${componentId}`);
}

/** Remove a component, its descendants, and every reference to it (grounding status `dropped`). */
function removeSubtree(state: StreamState, componentId: string): StreamState {
  const doomed = new Set<string>();
  const visit = (id: string) => {
    const node = state.components.get(id);
    if (!node || doomed.has(id)) return;
    doomed.add(id);
    childrenOf(node)?.forEach(visit);
  };
  visit(componentId);
  const components = new Map<string, CEIUIComponent>();
  for (const [id, node] of state.components) {
    if (doomed.has(id)) continue;
    const children = childrenOf(node);
    components.set(id, children ? ({ ...node, children: children.filter((child) => !doomed.has(child)) } as CEIUIComponent) : node);
  }
  return { ...state, components, rootIds: state.rootIds.filter((id) => !doomed.has(id)) };
}

const HANDLERS: HandlerTable = {
  surface_begin: (state, event) => ({
    ...initialStreamState,
    lastSeq: state.lastSeq,
    status: 'streaming',
    surface: { surface_id: event.surface_id, revision: event.revision, version: event.version, root_id: event.root_id },
  }),

  component_add: (state, event) => {
    if (!state.surface) return warn(state, 'component_add before surface_begin');
    const existing = state.components.get(event.component.id);
    if (existing) {
      // idempotent by id: identical re-add is a no-op, a different one is an update (protocol warning)
      if (JSON.stringify(existing) === JSON.stringify(withKnownChildren(event.component, state.components))) return state;
      return warn(applyPatch(state, existing.id, event.component as Record<string, unknown>), `component_add re-added ${existing.id} with different fields`);
    }
    const component = withKnownChildren(event.component, state.components);
    if (event.parent_id === null) {
      if (component.id !== state.surface.root_id || state.rootIds.length > 0) return warn(state, `unexpected root ${component.id}`);
      return { ...replaceComponent(state, component), rootIds: [component.id] };
    }
    const parent = state.components.get(event.parent_id);
    const siblings = parent ? childrenOf(parent) : null;
    if (!parent || !siblings) return warn(state, `unknown or non-container parent ${event.parent_id}`);
    const at = Math.min(event.index ?? siblings.length, siblings.length);
    const components = new Map(state.components);
    components.set(parent.id, { ...parent, children: [...siblings.slice(0, at), component.id, ...siblings.slice(at)] } as CEIUIComponent);
    components.set(component.id, component);
    return { ...state, components };
  },

  text_delta: (state, event) => {
    const target = state.components.get(event.component_id) as (CEIUIComponent & Record<string, unknown>) | undefined;
    if (!target || STRUCTURAL_FIELDS.has(event.field) || typeof target[event.field] !== 'string') {
      return warn(state, `text_delta for unknown text field ${event.component_id}.${event.field}`);
    }
    return replaceComponent(state, { ...target, [event.field]: (target[event.field] as string) + event.delta } as CEIUIComponent);
  },

  component_update: (state, event) => applyPatch(state, event.component_id, event.patch),

  grounding_update: (state, event) => {
    const target = state.components.get(event.component_id);
    if (!target) return warn(state, `grounding_update for unknown component ${event.component_id}`);
    const detail: GroundingState = { status: event.status };
    if (event.metrics) detail.metrics = event.metrics;
    if (event.flagged_cells) detail.flagged_cells = event.flagged_cells;
    let next: StreamState = { ...state, grounding: { ...state.grounding, [event.component_id]: detail } };
    if (event.status === 'dropped') return removeSubtree(next, event.component_id);
    if (event.citations && target.type === 'EvidencePanel') next = replaceComponent(next, { ...target, citations: event.citations });
    return next;
  },

  trace_event: (state, event) => ({ ...state, traceEvents: [...state.traceEvents, event] }),

  progress: (state, { seq: _seq, type: _type, ...progress }) => ({ ...state, progress }),

  usage: (state, { seq: _seq, type: _type, ...usage }) => ({ ...state, usage }),

  heartbeat: (state, event) => ({ ...state, lastHeartbeatAt: event.ts }),

  surface_end: (state) => ({ ...state, status: 'ended' }),

  error: (state, event) => ({ ...state, status: 'error', error: { code: event.code, message: event.message } }),

  hitl_request: (state, event) => ({
    ...state,
    status: 'awaiting_input',
    interactions: [...state.interactions, { kind: 'hitl', seq: event.seq, surface: event.surface }],
  }),

  job_handoff: (state, event) => ({
    ...state,
    status: 'handed_off',
    interactions: [
      ...state.interactions,
      { kind: 'handoff', seq: event.seq, job_id: event.job_id, reason: event.reason, poll_url: event.poll_url },
    ],
  }),
};

export function isStreamTerminal(state: StreamState): boolean {
  return state.status !== 'idle' && state.status !== 'streaming';
}

/**
 * Apply one event. Duplicate / out-of-order (`seq <= lastSeq`) and post-terminal events return
 * `state` unchanged (same reference); an `error` is still accepted after a non-error terminal event.
 */
export function reduceEvent(state: StreamState, event: StreamEvent): StreamState {
  if (isStreamTerminal(state) && (event.type !== 'error' || state.status === 'error')) return state;
  if (event.type === 'heartbeat') return HANDLERS.heartbeat(state, event); // SSE comment: no seq
  if (event.seq <= state.lastSeq) return state;
  const handler = HANDLERS[event.type] as Handler<typeof event.type>;
  return { ...handler(state, event as never), lastSeq: event.seq };
}

/** Parse and fold a raw event list; invalid or unknown events are skipped. */
export function reduceEvents(events: readonly unknown[], initial: StreamState = initialStreamState): StreamState {
  return events.reduce<StreamState>((state, raw) => {
    const event = parseStreamEvent(raw);
    return event ? reduceEvent(state, event) : state;
  }, initial);
}

/** Project state into the flat surface shape CEIUISurface renders, or null until the root exists. */
export function selectTree(state: StreamState): CEIUISurface | null {
  if (!state.surface || state.rootIds.length === 0) return null;
  return {
    content_type: CEI_UI_CONTENT_TYPE,
    protocol: CEI_UI_PROTOCOL,
    // 1.2 is a transport-only change; the replayed graph is a valid 1.1 surface (streaming-protocol.md).
    version: CEI_UI_VERSION,
    surface_id: state.surface.surface_id,
    revision: state.surface.revision,
    root_id: state.rootIds[0],
    components: [...state.components.values()],
  };
}

export { TERMINAL_EVENT_TYPES };
