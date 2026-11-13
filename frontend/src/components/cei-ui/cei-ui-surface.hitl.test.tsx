import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { CEIUISurface } from './cei-ui-surface';
import type { CEIUISurface as CEIUISurfaceType } from '../../lib/cei-ui/schema';

// CEIUISurface is the one renderer shared by the buffered path (LiveAgentChat's message.surface)
// and the stream path (StreamingSurface's hitl_request InteractionCard) - rendering it here once
// covers both call sites (StreamingSurface.test.tsx covers the stream-specific wiring).

const mocks = vi.hoisted(() => ({ dispatchCEIUIAction: vi.fn() }));
vi.mock('../../lib/cei-ui/actions', () => ({ dispatchCEIUIAction: mocks.dispatchCEIUIAction }));

afterEach(() => {
  cleanup();
  mocks.dispatchCEIUIAction.mockReset();
});

// Mirrors backend/agents/research/nodes.py build_clarify_surface (B72).
const CLARIFY_SURFACE: CEIUISurfaceType = {
  content_type: 'application/vnd.cei.ui+json',
  protocol: 'cei-ui',
  version: '1.1',
  surface_id: 'research-clarify',
  revision: 1,
  root_id: 'clarify-root',
  components: [
    { id: 'clarify-root', type: 'Column', children: ['clarify-intro', 'clarify-sources', 'clarify-rephrase'] },
    { id: 'clarify-intro', type: 'Text', variant: 'body', content: "I could not confidently match a source for: 'x'." },
    {
      id: 'clarify-sources',
      type: 'ChoicePicker',
      interaction_id: 'research-clarify-source',
      label: 'Which source should I search?',
      options: [{ label: 'PubMed', value: 'pubmed', description: null }, { label: 'openFDA', value: 'openfda', description: null }],
      multi: false,
      action: { action_id: 'select-clarify-source', label: 'Use this source', intent: 'submit' },
    },
    {
      id: 'clarify-rephrase',
      type: 'TextField',
      interaction_id: 'research-clarify-rephrase',
      label: 'Or rephrase your question',
      placeholder: 'For example: GLP-1 trials in type 2 diabetes',
      multiline: true,
      action: { action_id: 'submit-clarify-rephrase', label: 'Submit', intent: 'submit' },
    },
  ],
};

// Mirrors backend/agents/drafting/nodes.py _build_outline_approval_surface (B72).
const OUTLINE_SURFACE: CEIUISurfaceType = {
  content_type: 'application/vnd.cei.ui+json',
  protocol: 'cei-ui',
  version: '1.1',
  surface_id: 'drafting-outline',
  revision: 1,
  root_id: 'outline-root',
  components: [
    { id: 'outline-root', type: 'Column', children: ['outline-intro', 'outline-review', 'outline-edit'] },
    { id: 'outline-intro', type: 'Text', variant: 'body', content: 'Review the proposed outline before the full draft is written, or edit it below.' },
    {
      id: 'outline-review',
      type: 'ReviewGate',
      interaction_id: 'drafting-outline-review',
      plan: { summary: 'Proposed document outline', steps: [{ step_id: 'outline-0', title: 'Introduction', description: null, status: 'pending' }] },
      actions: [
        { action_id: 'approve-outline', label: 'Approve', intent: 'approve' },
        { action_id: 'reject-outline', label: 'Reject', intent: 'reject' },
      ],
      require_approval: true,
    },
    {
      id: 'outline-edit',
      type: 'TextField',
      interaction_id: 'drafting-outline-edit',
      label: 'Edit the outline (free text)',
      placeholder: '',
      multiline: true,
      action: { action_id: 'edit-outline', label: 'Submit edit', intent: 'submit' },
    },
  ],
};

describe('research clarify surface', () => {
  it('renders the source ChoicePicker and the free-text rephrase TextField', () => {
    render(<CEIUISurface surface={CLARIFY_SURFACE} agent="research" sessionId="s-1" />);
    expect(screen.getByText('Which source should I search?')).toBeTruthy();
    expect(screen.getByRole('option', { name: 'PubMed' })).toBeTruthy();
    expect(screen.getByLabelText('Or rephrase your question')).toBeTruthy();
  });

  it('posts the selected source via select-clarify-source', async () => {
    mocks.dispatchCEIUIAction.mockResolvedValue({ text: 'ok', status: 'completed' });
    render(<CEIUISurface surface={CLARIFY_SURFACE} agent="research" sessionId="s-1" />);
    fireEvent.change(screen.getByLabelText('Which source should I search?'), { target: { value: '1' } });
    fireEvent.click(screen.getByRole('button', { name: 'Use this source' }));
    await waitFor(() => expect(mocks.dispatchCEIUIAction).toHaveBeenCalledWith(expect.objectContaining({
      interaction_id: 'research-clarify-source', action_id: 'select-clarify-source', value: 'openfda',
    })));
  });

  it('posts the rephrased question via submit-clarify-rephrase', async () => {
    mocks.dispatchCEIUIAction.mockResolvedValue({ text: 'ok', status: 'completed' });
    render(<CEIUISurface surface={CLARIFY_SURFACE} agent="research" sessionId="s-1" />);
    fireEvent.change(screen.getByLabelText('Or rephrase your question'), { target: { value: 'GLP-1 trials' } });
    fireEvent.click(screen.getByRole('button', { name: 'Submit' }));
    await waitFor(() => expect(mocks.dispatchCEIUIAction).toHaveBeenCalledWith(expect.objectContaining({
      interaction_id: 'research-clarify-rephrase', action_id: 'submit-clarify-rephrase', value: 'GLP-1 trials',
    })));
  });
});

describe('drafting outline approval surface', () => {
  it('renders the plan steps and the approve/reject/edit controls', () => {
    render(<CEIUISurface surface={OUTLINE_SURFACE} agent="drafting" sessionId="s-1" />);
    expect(screen.getByText('Proposed document outline')).toBeTruthy();
    expect(screen.getByText('Introduction')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Approve' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Reject' })).toBeTruthy();
    expect(screen.getByLabelText('Edit the outline (free text)')).toBeTruthy();
  });

  it('posts approve-outline when Approve is clicked', async () => {
    mocks.dispatchCEIUIAction.mockResolvedValue({ text: 'ok', status: 'completed' });
    render(<CEIUISurface surface={OUTLINE_SURFACE} agent="drafting" sessionId="s-1" />);
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
    await waitFor(() => expect(mocks.dispatchCEIUIAction).toHaveBeenCalledWith(expect.objectContaining({
      interaction_id: 'drafting-outline-review', action_id: 'approve-outline',
    })));
  });

  it('posts the edited outline via edit-outline', async () => {
    mocks.dispatchCEIUIAction.mockResolvedValue({ text: 'ok', status: 'completed' });
    render(<CEIUISurface surface={OUTLINE_SURFACE} agent="drafting" sessionId="s-1" />);
    fireEvent.change(screen.getByLabelText('Edit the outline (free text)'), { target: { value: 'Add a methods section' } });
    fireEvent.click(screen.getByRole('button', { name: 'Submit edit' }));
    await waitFor(() => expect(mocks.dispatchCEIUIAction).toHaveBeenCalledWith(expect.objectContaining({
      interaction_id: 'drafting-outline-edit', action_id: 'edit-outline', value: 'Add a methods section',
    })));
  });

  it('disables every control when rendered as an already-answered hitl card', () => {
    render(<CEIUISurface surface={OUTLINE_SURFACE} agent="drafting" sessionId="s-1" disabled />);
    expect((screen.getByRole('button', { name: 'Approve' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByLabelText('Edit the outline (free text)') as HTMLTextAreaElement).disabled).toBe(true);
  });
});
