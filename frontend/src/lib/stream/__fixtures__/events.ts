// Sample stream event lists for reducer tests (no network, no live F5).
// Shapes follow .agents/specs/streaming-protocol.md; `seq` is the SSE `id:` folded in by sseParser.

export const CITATION = {
  citation_id: 'pmid-1',
  title: 'A study',
  source_type: 'pubmed',
  url: 'https://pubmed.ncbi.nlm.nih.gov/1/',
  year: 2024,
  relevant_passage: null,
};

export const BEGIN = {
  seq: 0,
  type: 'surface_begin',
  surface_id: 'research-turn-42',
  revision: 1,
  content_type: 'application/vnd.cei.ui+json',
  protocol: 'cei-ui',
  version: '1.2',
  root_id: 'root',
};

export const ADD_ROOT = { seq: 1, type: 'component_add', parent_id: null, component: { id: 'root', type: 'Column', children: [] } };

export const ADD_TEXT = {
  seq: 2,
  type: 'component_add',
  parent_id: 'root',
  index: 0,
  component: { id: 'answer', type: 'Markdown', content: '' },
};

export const ADD_EVIDENCE = {
  seq: 3,
  type: 'component_add',
  parent_id: 'root',
  index: 1,
  component: { id: 'evidence', type: 'EvidencePanel', summary: 'Sources', citations: [] },
};

export const ADD_STATS = {
  seq: 4,
  type: 'component_add',
  parent_id: 'root',
  component: { id: 'stats', type: 'StatGrid', metrics: [{ label: 'Trials', value: '12' }] },
};

export const END = { seq: 100, type: 'surface_end', surface_id: 'research-turn-42', revision: 1, root_id: 'root', final: true };

export const START: unknown[] = [BEGIN, ADD_ROOT];

export const FULL_RUN: unknown[] = [
  BEGIN,
  ADD_ROOT,
  ADD_TEXT,
  ADD_EVIDENCE,
  { seq: 5, type: 'progress', status: 'running', step_label: 'Searching PubMed', current_step: 1, total_steps: 3 },
  { seq: 6, type: 'trace_event', ts: '2026-09-29T00:00:03.210Z', step_type: 'tool_call', tool_name: 'pubmed___search', tool_input: { query: 'GLP-1' }, tool_output_summary: '12 results retrieved', sources: ['PMID:1'] },
  { seq: 7, type: 'text_delta', component_id: 'answer', field: 'content', delta: 'The review is comp' },
  { seq: 8, type: 'grounding_update', component_id: 'evidence', status: 'pending' },
  { seq: 9, type: 'text_delta', component_id: 'answer', delta: 'lete.' },
  { seq: 10, type: 'grounding_update', component_id: 'evidence', status: 'verified', citations: [CITATION] },
  { seq: 11, type: 'usage', input_tokens: 1820, output_tokens: 340, total_tokens: 2160, estimated_cost_usd: 0.0134 },
  END,
];
