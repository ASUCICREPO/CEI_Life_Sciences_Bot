import { describe, expect, it } from 'vitest';
import { STREAM_EVENT_TYPES, parseStreamEvent } from './events';
import { initialStreamState, reduceEvent, reduceEvents, selectTree, type StreamState } from './reducer';
import { ADD_EVIDENCE, ADD_ROOT, ADD_STATS, ADD_TEXT, BEGIN, CITATION, END, FULL_RUN, START } from './__fixtures__/events';

function component(state: StreamState, id: string) {
  return selectTree(state)?.components.find((item) => item.id === id);
}

function rootChildren(state: StreamState): string[] {
  const root = component(state, 'root');
  return root && 'children' in root ? root.children : [];
}

const HITL_SURFACE = {
  version: '1.2',
  surface_id: 'scope-1',
  root_id: 'pick',
  components: [{
    id: 'pick',
    type: 'ChoicePicker',
    interaction_id: 'i-1',
    label: 'Pick one',
    options: [{ label: 'A', value: 'a' }],
    action: { action_id: 'choose', label: 'Choose' },
  }],
};

describe('stream event contract', () => {
  it('covers all 13 event types', () => {
    expect(STREAM_EVENT_TYPES).toHaveLength(13);
  });

  it('returns null for unknown or invalid events instead of throwing', () => {
    expect(parseStreamEvent({ seq: 1, type: 'future_event' })).toBeNull();
    expect(parseStreamEvent({ type: 'usage', input_tokens: 1, output_tokens: 1 })).toBeNull(); // no seq
    expect(parseStreamEvent({ seq: 1, type: 'text_delta', component_id: 'a', delta: 'x'.repeat(4_097) })).toBeNull();
    expect(parseStreamEvent(null)).toBeNull();
    expect(parseStreamEvent('data')).toBeNull();
  });

  it('collapses unknown error codes to internal_error', () => {
    expect(parseStreamEvent({ seq: 1, type: 'error', code: 'boom', message: 'x' })).toMatchObject({ code: 'internal_error' });
  });
});

describe('surface_begin', () => {
  it('starts streaming; the tree appears once the root is added', () => {
    const begun = reduceEvents([BEGIN]);
    expect(begun.status).toBe('streaming');
    expect(begun.surface).toMatchObject({ surface_id: 'research-turn-42', version: '1.2', root_id: 'root' });
    expect(selectTree(begun)).toBeNull();
    expect(selectTree(reduceEvents(START))).toMatchObject({ surface_id: 'research-turn-42', root_id: 'root', version: '1.1' });
  });

  it('resets state from a previous surface', () => {
    const state = reduceEvents([...START, ADD_TEXT, { ...BEGIN, seq: 3, surface_id: 'research-turn-43' }]);
    expect(state.surface?.surface_id).toBe('research-turn-43');
    expect(state.components.size).toBe(0);
  });

  it('has no tree before surface_begin', () => {
    expect(selectTree(initialStreamState)).toBeNull();
  });
});

describe('surface_end', () => {
  it('keeps the persisted message id for loading this turn\'s trace', () => {
    const state = reduceEvents([...START, { ...END, message_id: 'turn-123' }]);
    expect(state.messageId).toBe('turn-123');
  });
});

describe('component_add', () => {
  it('accepts children declared by a Tabs parent without losing its labels', () => {
    const root = { seq: 1, type: 'component_add', parent_id: null, component: { id: 'root', type: 'Tabs', tabs: [{ label: 'Answer', child_id: 'answer' }] } };
    expect(selectTree(reduceEvents([BEGIN, root]))).toBeNull();
    const state = reduceEvents([
      BEGIN, root,
      { seq: 2, type: 'component_add', parent_id: 'root', component: { id: 'answer', type: 'Markdown', content: 'Hello' } },
    ]);
    expect(component(state, 'root')).toMatchObject({ tabs: [{ label: 'Answer', child_id: 'answer' }] });
    expect(component(state, 'answer')).toMatchObject({ content: 'Hello' });
    expect(state.warnings).toEqual([]);
  });
  it('splices components into the parent at index', () => {
    const state = reduceEvents([...START, ADD_EVIDENCE, { ...ADD_TEXT, seq: 4 }]);
    expect(rootChildren(state)).toEqual(['answer', 'evidence']);
  });

  it('appends when index is omitted', () => {
    expect(rootChildren(reduceEvents([...START, ADD_TEXT, ADD_STATS]))).toEqual(['answer', 'stats']);
  });

  it('drops components with an unknown parent', () => {
    const state = reduceEvents([...START, { ...ADD_TEXT, parent_id: 'missing' }]);
    expect(component(state, 'answer')).toBeUndefined();
    expect(state.warnings).toHaveLength(1);
  });

  it('rejects a second root', () => {
    const state = reduceEvents([...START, { ...ADD_ROOT, seq: 2, component: { id: 'other', type: 'Row', children: [] } }]);
    expect(state.rootIds).toEqual(['root']);
  });

  it('is idempotent by id: identical re-add is a no-op, different re-add is an update', () => {
    const same = reduceEvents([...START, ADD_TEXT, { ...ADD_TEXT, seq: 3 }]);
    expect(rootChildren(same)).toEqual(['answer']);
    expect(same.warnings).toEqual([]);
    const changed = reduceEvents([...START, ADD_TEXT, { ...ADD_TEXT, seq: 3, component: { id: 'answer', type: 'Markdown', content: 'v2' } }]);
    expect(rootChildren(changed)).toEqual(['answer']);
    expect(component(changed, 'answer')).toMatchObject({ content: 'v2' });
    expect(changed.warnings).toHaveLength(1);
  });
});

describe('text_delta', () => {
  it('appends to the target field in order', () => {
    expect(component(reduceEvents(FULL_RUN), 'answer')).toMatchObject({ content: 'The review is complete.' });
  });

  it('ignores deltas for components without that text field', () => {
    const state = reduceEvents([...START, { seq: 2, type: 'text_delta', component_id: 'root', delta: 'x' }]);
    expect(component(state, 'root')).not.toHaveProperty('content');
    expect(state.warnings).toHaveLength(1);
  });
});

describe('component_update', () => {
  it('shallow-merges patch', () => {
    const state = reduceEvents([...START, ADD_EVIDENCE, { seq: 4, type: 'component_update', component_id: 'evidence', patch: { summary: 'Updated' } }]);
    expect(component(state, 'evidence')).toMatchObject({ summary: 'Updated', type: 'EvidencePanel' });
  });

  it('refuses structural or invalid changes', () => {
    const state = reduceEvents([
      ...START,
      ADD_TEXT,
      { seq: 3, type: 'component_update', component_id: 'answer', patch: { type: 'Button', id: 'other' } },
      { seq: 4, type: 'component_update', component_id: 'answer', patch: { content: 42 } },
    ]);
    expect(component(state, 'answer')).toMatchObject({ id: 'answer', type: 'Markdown', content: '' });
    expect(state.warnings).toHaveLength(1);
  });
});

describe('grounding_update', () => {
  it('shows pending, then the final status with citations', () => {
    const pending = reduceEvents(FULL_RUN.slice(0, 8));
    expect(pending.grounding.evidence).toEqual({ status: 'pending' });
    const resolved = reduceEvents(FULL_RUN);
    expect(resolved.grounding.evidence).toEqual({ status: 'verified' });
    expect(component(resolved, 'evidence')).toMatchObject({ citations: [CITATION] });
  });

  it('keeps per-metric and per-cell detail', () => {
    const state = reduceEvents([...START, ADD_STATS, {
      seq: 5, type: 'grounding_update', component_id: 'stats', status: 'partially_verified',
      metrics: [{ index: 0, verified: false }], flagged_cells: [{ row: 1, column: 1 }],
    }]);
    expect(state.grounding.stats).toEqual({
      status: 'partially_verified', metrics: [{ index: 0, verified: false }], flagged_cells: [{ row: 1, column: 1 }],
    });
  });

  it('removes a dropped component from the graph', () => {
    const state = reduceEvents([...START, ADD_TEXT, ADD_STATS, { seq: 5, type: 'grounding_update', component_id: 'stats', status: 'dropped' }]);
    expect(component(state, 'stats')).toBeUndefined();
    expect(rootChildren(state)).toEqual(['answer']);
    expect(state.grounding.stats).toEqual({ status: 'dropped' });
  });
});

describe('trace_event', () => {
  it('appends to traceEvents in order without touching the graph', () => {
    const before = reduceEvents(START);
    const state = reduceEvents([
      { seq: 2, type: 'trace_event', step_type: 'plan', reasoning_text: 'Plan' },
      { seq: 3, type: 'trace_event', step_type: 'reasoning', reasoning_text: 'Checked two trials', reasoning_label: 'summarized' },
    ], before);
    expect(state.traceEvents.map((event) => event.step_type)).toEqual(['plan', 'reasoning']);
    expect(state.components).toBe(before.components);
  });
});

describe('progress', () => {
  it('replaces the progress slice', () => {
    const state = reduceEvents([...START, { seq: 2, type: 'progress', status: 'planning' }, { seq: 3, type: 'progress', status: 'verifying', step_label: 'Checking' }]);
    expect(state.progress).toEqual({ status: 'verifying', step_label: 'Checking' });
  });
});

describe('usage', () => {
  it('replaces the usage slice', () => {
    expect(reduceEvents(FULL_RUN).usage).toEqual({ input_tokens: 1820, output_tokens: 340, total_tokens: 2160, estimated_cost_usd: 0.0134 });
  });
});

describe('heartbeat', () => {
  it('updates only lastHeartbeatAt and consumes no seq', () => {
    const before = reduceEvents([...START, ADD_TEXT]);
    const after = reduceEvents([{ type: 'heartbeat', ts: 'T1' }], before);
    expect(after.lastHeartbeatAt).toBe('T1');
    expect(after.lastSeq).toBe(before.lastSeq);
    expect({ ...after, lastHeartbeatAt: null }).toEqual(before);
  });
});

describe('surface_end', () => {
  it('sets ended', () => {
    expect(reduceEvents(FULL_RUN).status).toBe('ended');
  });
});

describe('error', () => {
  it('sets error status and details', () => {
    const state = reduceEvents([...START, { seq: 2, type: 'error', code: 'rate_limited', message: 'The request could not be completed.' }]);
    expect(state.status).toBe('error');
    expect(state.error).toEqual({ code: 'rate_limited', message: 'The request could not be completed.' });
  });

  it('is still accepted after surface_end, but nothing is accepted after error', () => {
    const state = reduceEvents([...START, END, { seq: 101, type: 'error', code: 'internal_error', message: 'x' }]);
    expect(state.status).toBe('error');
    const event = parseStreamEvent({ seq: 102, type: 'error', code: 'internal_error', message: 'y' });
    expect(reduceEvent(state, event!)).toBe(state);
  });
});

describe('hitl_request', () => {
  it('stores the render-only 1.2 surface and pauses the stream', () => {
    const state = reduceEvents([...START, { seq: 2, type: 'hitl_request', surface: HITL_SURFACE }]);
    expect(state.status).toBe('awaiting_input');
    expect(state.interactions).toHaveLength(1);
    expect(state.interactions[0]).toMatchObject({ kind: 'hitl', seq: 2, surface: { surface_id: 'scope-1', root_id: 'pick' } });
  });
});

describe('job_handoff', () => {
  it('adds a render-only handoff node and ends this stream', () => {
    const state = reduceEvents([BEGIN, { seq: 1, type: 'job_handoff', job_id: 'job-1', reason: 'estimated_duration_exceeded', poll_url: '/jobs/job-1' }]);
    expect(state.status).toBe('handed_off');
    expect(state.interactions).toEqual([{ kind: 'handoff', seq: 1, job_id: 'job-1', reason: 'estimated_duration_exceeded', poll_url: '/jobs/job-1' }]);
  });
});

describe('ordering rules', () => {
  it('ignores a duplicate seq and returns the same reference', () => {
    const state = reduceEvents([...START, ADD_TEXT]);
    const event = parseStreamEvent({ seq: 2, type: 'text_delta', component_id: 'answer', delta: 'dup' });
    expect(event).not.toBeNull();
    expect(reduceEvent(state, event!)).toBe(state);
  });

  it('ignores a lower seq after a higher one', () => {
    const state = reduceEvents([
      ...START,
      ADD_TEXT,
      { seq: 5, type: 'text_delta', component_id: 'answer', delta: 'B' },
      { seq: 4, type: 'text_delta', component_id: 'answer', delta: 'A' },
    ]);
    expect(component(state, 'answer')).toMatchObject({ content: 'B' });
    expect(state.lastSeq).toBe(5);
  });

  it('accepts gaps in seq', () => {
    const state = reduceEvents([...START, ADD_TEXT, { seq: 9, type: 'text_delta', component_id: 'answer', delta: 'gap' }]);
    expect(component(state, 'answer')).toMatchObject({ content: 'gap' });
  });

  it('ignores events after a terminal event', () => {
    const ended = reduceEvents(FULL_RUN);
    const event = parseStreamEvent({ seq: 101, type: 'text_delta', component_id: 'answer', delta: '!' });
    expect(reduceEvent(ended, event!)).toBe(ended);
    const paused = reduceEvents([...START, { seq: 2, type: 'hitl_request', surface: HITL_SURFACE }]);
    expect(reduceEvent(paused, parseStreamEvent(ADD_TEXT)!)).toBe(paused);
  });

  it('gives the same tree when a prefix is replayed on reconnect (B68)', () => {
    const clean = reduceEvents(FULL_RUN);
    const replayed = reduceEvents([...FULL_RUN.slice(0, 8), ...FULL_RUN.slice(2, 8), ...FULL_RUN.slice(8)]);
    expect(selectTree(replayed)).toEqual(selectTree(clean));
    expect(replayed.traceEvents).toEqual(clean.traceEvents);
  });

  it('skips invalid and unknown events', () => {
    const state = reduceEvents([...START, { seq: 2, type: 'mystery' }, 'garbage', ADD_TEXT]);
    expect(rootChildren(state)).toEqual(['answer']);
    expect(state.lastSeq).toBe(2);
  });
});
