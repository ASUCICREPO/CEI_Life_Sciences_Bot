import { z } from 'zod';
import { CEIUISurfaceSchema, CitationDescriptorSchema, ComponentSchema } from '../cei-ui/schema';

// Typed contract for the CEI UI 1.2 progressive-patch stream. Binding definition:
// .agents/specs/streaming-protocol.md ("Event catalog"). Component payloads reuse the
// CEI UI 1.1 component contract from ../cei-ui/schema rather than redefining it.
//
// Envelope: the SSE frame's `id:` is the event's `seq` and `event:` its `type`; the
// SSE layer (sseParser) folds them into the parsed object as `{ seq, type, ...data }`.
// `heartbeat` is an SSE comment line, so it has no `seq` and never consumes one.

const seqSchema = z.int().nonnegative();
const idSchema = z.string().min(1).max(128);

export const MAX_TEXT_DELTA_BYTES = 4_096; // B78
export const STREAM_SURFACE_VERSIONS = ['1.0', '1.1', '1.2'] as const;

const base = { seq: seqSchema };

export const SurfaceBeginEventSchema = z.object({
  ...base,
  type: z.literal('surface_begin'),
  surface_id: z.string().min(1).max(128),
  revision: z.int().positive().default(1),
  version: z.enum(STREAM_SURFACE_VERSIONS).default('1.2'),
  root_id: idSchema,
});

export const ComponentAddEventSchema = z.object({
  ...base,
  type: z.literal('component_add'),
  parent_id: idSchema.nullable(),
  index: z.int().nonnegative().optional(),
  component: ComponentSchema,
});

export const TextDeltaEventSchema = z.object({
  ...base,
  type: z.literal('text_delta'),
  component_id: idSchema,
  field: z.string().min(1).max(64).default('content'),
  delta: z.string().refine(
    (value) => new TextEncoder().encode(value).length <= MAX_TEXT_DELTA_BYTES,
    `text_delta exceeds ${MAX_TEXT_DELTA_BYTES} bytes`,
  ),
});

export const ComponentUpdateEventSchema = z.object({
  ...base,
  type: z.literal('component_update'),
  component_id: idSchema,
  patch: z.record(z.string(), z.unknown()),
});

export const GroundingStatusSchema = z.enum([
  'pending',
  'verified',
  'partially_verified',
  'unverified',
  'dropped',
  'estimate',
]);

export const GroundingUpdateEventSchema = z.object({
  ...base,
  type: z.literal('grounding_update'),
  component_id: idSchema,
  status: GroundingStatusSchema,
  metrics: z.array(z.object({ index: z.int().nonnegative(), verified: z.boolean() })).optional(),
  flagged_cells: z.array(z.object({ row: z.int().nonnegative(), column: z.int().nonnegative() })).optional(),
  citations: z.array(CitationDescriptorSchema).optional(),
});

export const TraceStepTypeSchema = z.enum(['plan', 'tool_call', 'source_access', 'grounding_decision', 'reasoning']);

export const TraceEventSchema = z.object({
  ...base,
  type: z.literal('trace_event'),
  ts: z.string().optional(),
  step_type: TraceStepTypeSchema,
  tool_name: z.string().max(128).optional(),
  tool_input: z.record(z.string(), z.unknown()).optional(),
  tool_output_summary: z.string().optional(),
  sources: z.array(z.string()).optional(),
  reasoning_text: z.string().optional(),
  reasoning_label: z.enum(['summarized', 'full']).optional(),
});

export const ProgressEventSchema = z.object({
  ...base,
  type: z.literal('progress'),
  status: z.enum(['planning', 'running', 'verifying', 'finalizing']),
  step_label: z.string().max(512).optional(),
  current_step: z.int().nonnegative().optional(),
  total_steps: z.int().nonnegative().optional(),
});

export const UsageEventSchema = z.object({
  ...base,
  type: z.literal('usage'),
  input_tokens: z.int().nonnegative(),
  output_tokens: z.int().nonnegative(),
  total_tokens: z.int().nonnegative().optional(),
  estimated_cost_usd: z.number().nonnegative().optional(),
});

// SSE comment (`:heartbeat`), synthesized client-side; carries the local receive time.
export const HeartbeatEventSchema = z.object({
  type: z.literal('heartbeat'),
  ts: z.string(),
});

export const SurfaceEndEventSchema = z.object({
  ...base,
  type: z.literal('surface_end'),
  surface_id: z.string().optional(),
  revision: z.int().positive().optional(),
  root_id: z.string().optional(),
  final: z.boolean().default(true),
});

const ERROR_CODES = ['invalid_request', 'rate_limited', 'service_unavailable', 'internal_error'] as const;

export const ErrorEventSchema = z.object({
  ...base,
  type: z.literal('error'),
  message: z.string().max(1_000),
  // any other code collapses to internal_error (same rule as shared/transport/sse.py)
  code: z.preprocess((value) => (ERROR_CODES.includes(value as never) ? value : 'internal_error'), z.enum(ERROR_CODES)),
});

// The 1.1 surface schema accepts 1.0/1.1; a 1.2 surface is the same component graph (transport-only change).
const streamedSurfaceSchema = z.preprocess(
  (value) => (value && typeof value === 'object' && (value as { version?: unknown }).version === '1.2'
    ? { ...(value as object), version: '1.1' }
    : value),
  CEIUISurfaceSchema,
);

// Terminal (pause). Render-only here: the reducer stores the surface; it never posts /ui-actions (A5).
export const HitlRequestEventSchema = z.object({
  ...base,
  type: z.literal('hitl_request'),
  surface: streamedSurfaceSchema,
});

// Terminal (handoff). Render-only here: the reducer shows a card; it never starts polling (A5).
export const JobHandoffEventSchema = z.object({
  ...base,
  type: z.literal('job_handoff'),
  job_id: idSchema,
  reason: z.string().max(128).optional(),
  poll_url: z.string().max(512).optional(),
});

export const StreamEventSchema = z.discriminatedUnion('type', [
  SurfaceBeginEventSchema,
  ComponentAddEventSchema,
  TextDeltaEventSchema,
  ComponentUpdateEventSchema,
  GroundingUpdateEventSchema,
  TraceEventSchema,
  ProgressEventSchema,
  UsageEventSchema,
  HeartbeatEventSchema,
  SurfaceEndEventSchema,
  ErrorEventSchema,
  HitlRequestEventSchema,
  JobHandoffEventSchema,
]);

export type StreamEvent = z.infer<typeof StreamEventSchema>;
export type StreamEventType = StreamEvent['type'];
export type StreamEventOf<T extends StreamEventType> = Extract<StreamEvent, { type: T }>;
export type SequencedStreamEvent = Exclude<StreamEvent, { type: 'heartbeat' }>;
export type TraceEvent = StreamEventOf<'trace_event'>;
export type GroundingStatus = z.infer<typeof GroundingStatusSchema>;

export const STREAM_EVENT_TYPES = StreamEventSchema.options.map((option) => option.shape.type.value) as StreamEventType[];

/** Events after which the server ends the HTTP response (streaming-protocol.md ordering rule 6). */
export const TERMINAL_EVENT_TYPES: ReadonlySet<StreamEventType> = new Set(['surface_end', 'hitl_request', 'job_handoff', 'error']);

/** Parse one raw event. Unknown or invalid events return null; never throws. */
export function parseStreamEvent(raw: unknown): StreamEvent | null {
  const result = StreamEventSchema.safeParse(raw);
  return result.success ? result.data : null;
}
