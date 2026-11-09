import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { StreamingSurface } from './StreamingSurface';
import { parseStreamEvent, type StreamEvent } from '../../lib/stream/events';
import type { OpenStreamOptions } from '../../lib/stream/openStream';
import { FULL_RUN } from '../../lib/stream/__fixtures__/events';

vi.mock('../../lib/auth/AuthProvider', () => ({
  useAuth: () => ({ getAccessToken: async () => 'tok', config: { apiBaseUrl: 'https://api.example.test/prod/' } }),
}));

afterEach(cleanup);

function scripted(raw: unknown[], { hang = false } = {}) {
  const calls: OpenStreamOptions[] = [];
  const impl = (options: OpenStreamOptions): AsyncIterable<StreamEvent> => {
    calls.push(options);
    return (async function* () {
      for (const item of raw) {
        const event = parseStreamEvent(item);
        if (event) yield event;
      }
      if (hang) await new Promise((resolve) => options.signal?.addEventListener('abort', resolve));
    })();
  };
  return { calls, impl };
}

describe('StreamingSurface', () => {
  it('renders the streamed tree through CEIUISurface', async () => {
    const { calls, impl } = scripted(FULL_RUN);
    render(<StreamingSurface sessionId="s-1" agent="research" body={{ prompt: 'hi' }} openStreamImpl={impl} />);
    expect(await screen.findByText('The review is complete.')).toBeTruthy();
    // Streaming must pass through the same-origin CloudFront/WAF behavior.
    expect(calls[0].url).toBe('/chat/stream');
    expect(calls[0].body).toEqual({ prompt: 'hi' });
    await waitFor(() => expect(document.querySelector('[data-status="ended"]')).not.toBeNull());
  });

  it('shows the grounding pending state and progress while streaming', async () => {
    const { impl } = scripted(FULL_RUN.slice(0, 8), { hang: true });
    render(<StreamingSurface sessionId="s-1" agent="research" body={{}} openStreamImpl={impl} />);
    expect(await screen.findByTestId('grounding-pending')).toBeTruthy();
    expect(screen.getByText(/Working — Searching PubMed \(1\/3\)/)).toBeTruthy();
  });

  it('renders hitl_request and job_handoff as render-only cards', async () => {
    const { impl } = scripted([
      { seq: 0, type: 'surface_begin', surface_id: 's', root_id: 'root' },
      { seq: 1, type: 'job_handoff', job_id: 'job-9', poll_url: '/jobs/job-9' },
    ]);
    render(<StreamingSurface sessionId="s-1" agent="pipeline" body={{}} openStreamImpl={impl} />);
    expect(await screen.findByText(/Job job-9/)).toBeTruthy();
  });

  it('shows the redacted error message', async () => {
    const { impl } = scripted([
      { seq: 0, type: 'surface_begin', surface_id: 's', root_id: 'root' },
      { seq: 1, type: 'error', code: 'rate_limited', message: 'The request could not be completed.' },
    ]);
    render(<StreamingSurface sessionId="s-1" agent="research" body={{}} openStreamImpl={impl} />);
    expect((await screen.findByRole('alert')).textContent).toBe('The request could not be completed.');
  });

  it('reports completion exactly once to the owning chat', async () => {
    const { impl } = scripted(FULL_RUN);
    const finished = vi.fn();
    render(<StreamingSurface sessionId="s-1" agent="research" body={{ message: 'hi' }} openStreamImpl={impl} onFinished={finished} />);
    await waitFor(() => expect(finished).toHaveBeenCalledTimes(1));
    expect(finished.mock.calls[0][0].status).toBe('ended');
    expect(finished.mock.calls[0][1]).toBeNull();
  });

  it('reports connection failure so the chat can unlock', async () => {
    const finished = vi.fn();
    const impl = async function* (): AsyncGenerator<StreamEvent> { throw new Error('offline'); };
    render(<StreamingSurface sessionId="s-1" agent="research" body={{}} openStreamImpl={impl} onFinished={finished} />);
    await waitFor(() => expect(finished).toHaveBeenCalledTimes(1));
    expect(finished.mock.calls[0][1]).toBe('The response stream failed.');
  });

  it('aborts the stream on unmount', async () => {
    const { calls, impl } = scripted([], { hang: true });
    const { unmount } = render(<StreamingSurface sessionId="s-1" agent="research" body={{}} openStreamImpl={impl} />);
    await waitFor(() => expect(calls).toHaveLength(1));
    unmount();
    expect(calls[0].signal?.aborted).toBe(true);
  });
});
