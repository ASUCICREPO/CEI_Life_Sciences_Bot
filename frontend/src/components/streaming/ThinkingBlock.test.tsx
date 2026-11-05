import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { ThinkingBlock } from './ThinkingBlock';
import { StreamingSurface } from './StreamingSurface';
import { parseStreamEvent, type StreamEvent, type TraceEvent } from '../../lib/stream/events';
import { createSessionsClient } from '../../lib/sessionsClient';
import type { OpenStreamOptions } from '../../lib/stream/openStream';

vi.mock('../../lib/auth/AuthProvider', () => ({
  useAuth: () => ({ getAccessToken: async () => 'tok', config: {} }),
}));

afterEach(cleanup);

function trace(seq: number, extra: Record<string, unknown>): TraceEvent {
  const event = parseStreamEvent({ seq, type: 'trace_event', ...extra });
  if (event?.type !== 'trace_event') throw new Error('bad fixture');
  return event;
}

const LIVE = [
  trace(1, { step_type: 'plan', reasoning_text: 'Search PubMed, then rank' }),
  trace(2, { step_type: 'tool_call', tool_name: 'pubmed___search', tool_input: { query: 'GLP-1' }, tool_output_summary: '12 results retrieved', sources: ['PMID:1'] }),
];

describe('ThinkingBlock', () => {
  it('is expanded with live events while streaming', () => {
    render(<ThinkingBlock traceEvents={LIVE} status="streaming" sessionId="s" loadTrace={vi.fn()} />);
    expect(screen.getByRole('button').getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByText('Search PubMed, then rank')).toBeTruthy();
    expect(screen.getByText('Tool · pubmed___search')).toBeTruthy();
    expect(screen.getByText('Sources: PMID:1')).toBeTruthy();
  });

  it('collapses when the stream ends and lazily fetches the stored trace on expand (B59)', async () => {
    const loadTrace = vi.fn(async () => [trace(0, { step_type: 'reasoning', reasoning_text: 'stored reasoning', reasoning_label: 'summarized' })]);
    const { rerender } = render(<ThinkingBlock traceEvents={LIVE} status="streaming" sessionId="s" messageId="m1" loadTrace={loadTrace} />);
    rerender(<ThinkingBlock traceEvents={LIVE} status="ended" sessionId="s" messageId="m1" loadTrace={loadTrace} />);
    expect(screen.getByRole('button').getAttribute('aria-expanded')).toBe('false');
    expect(loadTrace).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button'));
    expect(await screen.findByText('stored reasoning')).toBeTruthy();
    expect(loadTrace).toHaveBeenCalledWith('s', 'm1');

    fireEvent.click(screen.getByRole('button'));
    fireEvent.click(screen.getByRole('button'));
    expect(loadTrace).toHaveBeenCalledTimes(1);
  });

  it('falls back to live events when the stored trace cannot be loaded', async () => {
    const loadTrace = vi.fn(async () => { throw new Error('404'); });
    render(<ThinkingBlock traceEvents={LIVE} status="ended" sessionId="s" messageId="m1" loadTrace={loadTrace} />);
    fireEvent.click(screen.getByRole('button'));
    expect(await screen.findByText('Could not load the saved trace.')).toBeTruthy();
    expect(screen.getByText('Search PubMed, then rank')).toBeTruthy();
  });

  it('renders nothing for a turn with no trace and no message id', () => {
    const { container } = render(<ThinkingBlock traceEvents={[]} status="ended" sessionId="s" />);
    expect(container.innerHTML).toBe('');
  });
});

describe('B7 never-show', () => {
  const LEAKS = [
    'anthropic.claude-3-5-sonnet-20240620-v1:0',
    'us.anthropic.claude-sonnet-4-20250514-v1:0',
    'arn:aws:bedrock:us-east-1:123456789012:guardrail/abc',
    'guardrail gr-1 v2',
    'You are CEI. SYSTEM PROMPT v7',
    'prompt_version=7',
    'arn:aws:secretsmanager:us-east-1:1:secret:ncbi',
    'AKIAABCDEFGHIJKLMNOP',
    'sk-abcdefghijklmnop',
    'Bearer eyJhbGciOi',
  ];

  it('never renders model ids, guardrails, system prompts, prompt versions or secrets', async () => {
    const raw = [
      // fields outside the trace_event contract are stripped by the schema
      { seq: 1, type: 'trace_event', step_type: 'reasoning', reasoning_text: 'ok', model_id: LEAKS[0], guardrail_config: { id: LEAKS[2] }, system_prompt: LEAKS[4], prompt_version: '7' },
      // never-show keys inside tool_input are dropped; leaking values are redacted
      { seq: 2, type: 'trace_event', step_type: 'tool_call', tool_name: 'search', tool_input: { query: 'q', model_id: 'x', api_key: 'k', nested: { system_prompt: 's', auth: LEAKS[9] } }, tool_output_summary: LEAKS[1] },
      ...LEAKS.map((leak, index) => ({ seq: 3 + index, type: 'trace_event', step_type: 'reasoning', reasoning_text: leak, sources: [leak] })),
    ];
    const events = raw.map(parseStreamEvent).filter((event): event is TraceEvent => event?.type === 'trace_event');
    expect(events).toHaveLength(raw.length);

    const { container } = render(<ThinkingBlock traceEvents={events} status="streaming" sessionId="s" loadTrace={vi.fn()} />);
    const html = container.innerHTML;
    for (const leak of LEAKS) expect(html).not.toContain(leak);
    for (const key of ['model_id', 'api_key', 'system_prompt', 'guardrail_config', 'prompt_version']) expect(html).not.toContain(key);
    expect(screen.getAllByText('[redacted]').length).toBeGreaterThan(0);
    expect(html).toContain('{"query":"q","nested":{"auth":"[redacted]"}}');
  });

  it('applies to the lazily loaded stored trace too', async () => {
    const fetchImpl = vi.fn(async () => new Response(JSON.stringify({
      message_id: 'm1',
      events: [{ seq: 0, type: 'trace_event', step_type: 'reasoning', reasoning_text: LEAKS[1], model_id: LEAKS[0] }],
    }), { status: 200 })) as unknown as typeof fetch;
    const client = createSessionsClient(async () => 'tok', { fetchImpl, baseUrl: 'https://api.example' });
    const loadTrace = async (sid: string, mid: string) => (await client.loadTurnTrace(sid, mid)).events;
    const { container } = render(<ThinkingBlock traceEvents={[]} status="ended" sessionId="s-1" messageId="m1" loadTrace={loadTrace} />);
    fireEvent.click(screen.getByRole('button'));
    await screen.findByText('[redacted]');
    expect(container.innerHTML).not.toContain('anthropic');
    expect((fetchImpl as unknown as { mock: { calls: [string][] } }).mock.calls[0][0]).toBe('https://api.example/sessions/s-1/turns/m1/trace');
  });
});

describe('StreamingSurface + ThinkingBlock', () => {
  it('collapses the thinking block when surface_end arrives', async () => {
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    const impl = (options: OpenStreamOptions): AsyncIterable<StreamEvent> => (async function* () {
      const raw = [
        { seq: 0, type: 'surface_begin', surface_id: 's', root_id: 'root' },
        { seq: 1, type: 'trace_event', step_type: 'plan', reasoning_text: 'Planning the search' },
      ];
      for (const item of raw) yield parseStreamEvent(item)!;
      await gate;
      if (!options.signal?.aborted) yield parseStreamEvent({ seq: 2, type: 'surface_end' })!;
    })();
    render(<StreamingSurface sessionId="s" agent="research" body={{}} openStreamImpl={impl} />);
    expect(await screen.findByText('Planning the search')).toBeTruthy();
    expect(screen.getByRole('button', { name: /Thinking/ }).getAttribute('aria-expanded')).toBe('true');
    await act(async () => release());
    await waitFor(() => expect(screen.getByRole('button', { name: /Show reasoning/ }).getAttribute('aria-expanded')).toBe('false'));
  });
});
