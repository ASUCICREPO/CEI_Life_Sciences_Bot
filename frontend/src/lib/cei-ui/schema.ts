import { z } from 'zod';

export const CEI_UI_CONTENT_TYPE = 'application/vnd.cei.ui+json' as const;
export const CEI_UI_PROTOCOL = 'cei-ui' as const;
export const CEI_UI_VERSION = '1.1' as const;
export const CEI_UI_SUPPORTED_VERSIONS = ['1.0', '1.1'] as const;
export const MAX_COMPONENTS = 250;
export const MAX_COMPONENT_DEPTH = 32;
export const MAX_CHILDREN = 250;
export const MAX_ACTIONS = 20;
export const MAX_TABS = 20;
export const MAX_OPTIONS = 100;
export const MAX_CITATIONS = 100;
export const MAX_PLAN_STEPS = 100;
export const MAX_GRAPH_SERIES = 20;
export const MAX_GRAPH_CATEGORIES = 1_000;
export const MAX_GRAPH_VALUES = 1_000;
export const MAX_GRAPH_TOTAL_POINTS = 5_000;
export const MAX_ACTION_VALUE_DEPTH = 8;
export const MAX_ACTION_VALUE_NODES = 1_000;
export const MAX_ACTION_VALUE_MAPPING_ENTRIES = 100;
export const MAX_ACTION_VALUE_ARRAY_ENTRIES = 100;
export const MAX_ACTION_VALUE_KEY_LENGTH = 128;
export const MAX_ACTION_VALUE_STRING_LENGTH = 10_000;
export const MAX_TABLE_COLUMNS = 12;
export const MAX_TABLE_ROWS = 200;
export const MAX_TABLE_CELLS = 2_000;
export const MAX_METRICS = 12;
export const MAX_TIMELINE_ITEMS = 50;
export const MAX_COMPARISON_OPTIONS = 6;
export const MAX_COMPARISON_CRITERIA = 30;
export const MAX_ARTIFACT_HTML_CHARS = 65_536;
export const MAX_ARTIFACT_DATA_BYTES = 65_536;
export const MAX_ARTIFACTS_PER_SURFACE = 4;
export const MAX_ARTIFACT_TOTAL_BYTES = 262_144;
export const ARTIFACT_MIN_HEIGHT = 120;
export const ARTIFACT_MAX_HEIGHT = 1_200;
export const ARTIFACT_DEFAULT_HEIGHT = 420;

// Defense-in-depth lint for model-authored artifact HTML. Mirrors
// backend/agents/shared/schemas/cei_ui.py ARTIFACT_HTML_DENYLIST exactly; the
// sandboxed iframe and its CSP are the real security boundary.
export const ARTIFACT_HTML_DENYLIST = [
  '<\\s*/?\\s*(html|head|meta|base|link|iframe|frame|frameset|object|embed|portal|form|a)(\\s|/|>)',
  'https?:',
  '//[a-z0-9-]+\\.[a-z]',
  'javascript:',
  'srcdoc',
  '<\\s*script[^>]*\\bsrc\\s*=',
  '\\b(fetch|importScripts|import)\\s*\\(',
  '\\b(XMLHttpRequest|WebSocket|EventSource|sendBeacon|RTCPeerConnection)\\b',
  '\\bnew\\s+(Shared)?Worker\\b',
  "\\b(window|self|top|parent|opener|frames|globalThis)\\s*\\.\\s*(location|postMessage|open|opener|parent|top|document)\\b",
  '\\blocation\\s*\\.\\s*(href|assign|replace|reload)\\b',
  '\\blocation\\s*=',
  '\\bpostMessage\\b',
  '\\b(localStorage|sessionStorage|indexedDB)\\b',
  'document\\s*\\.\\s*(cookie|domain|write)',
] as const;

const ARTIFACT_DENYLIST_PATTERNS = ARTIFACT_HTML_DENYLIST.map((pattern) => new RegExp(pattern, 'i'));

export function findDeniedArtifactHtmlPatterns(value: string): string[] {
  return ARTIFACT_DENYLIST_PATTERNS.filter((pattern) => pattern.test(value)).map((pattern) => pattern.source);
}

const componentIdSchema = z.string().min(1).max(128);
const interactionIdSchema = z.string().min(1).max(128);
const componentTextSchema = z.string();
const markdownTextSchema = z.string();
const bodyTextSchema = z.string();
const summaryTextSchema = z.string();
const titleTextSchema = z.string();
const labelTextSchema = z.string();
const httpsUrlSchema = z.string().refine((value) => {
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && url.hostname.length > 0;
  } catch {
    return false;
  }
}, 'URLs must use HTTPS and include a host');

export type JsonScalar = string | number | boolean | null;
export type JsonValue = JsonScalar | JsonValue[] | { [key: string]: JsonValue };

const jsonValueSchema: z.ZodType<JsonValue> = z.lazy(() => z.union([
  z.string().max(MAX_ACTION_VALUE_STRING_LENGTH),
  z.number(),
  z.boolean(),
  z.null(),
  z.array(jsonValueSchema).max(MAX_ACTION_VALUE_ARRAY_ENTRIES),
  z.record(z.string().max(MAX_ACTION_VALUE_KEY_LENGTH), jsonValueSchema).refine(
    (value) => Object.keys(value).length <= MAX_ACTION_VALUE_MAPPING_ENTRIES,
    `action value mapping exceeds ${MAX_ACTION_VALUE_MAPPING_ENTRIES} entries`,
  ),
]));

export const JsonValueSchema = jsonValueSchema.superRefine((value, context) => {
  let nodeCount = 0;

  function visit(node: JsonValue, depth: number): boolean {
    if (depth > MAX_ACTION_VALUE_DEPTH) {
      context.addIssue({ code: 'custom', message: `action value depth exceeds ${MAX_ACTION_VALUE_DEPTH}` });
      return false;
    }
    nodeCount += 1;
    if (nodeCount > MAX_ACTION_VALUE_NODES) {
      context.addIssue({ code: 'custom', message: `action value nodes exceed ${MAX_ACTION_VALUE_NODES}` });
      return false;
    }
    if (Array.isArray(node)) return node.every((item) => visit(item, depth + 1));
    if (typeof node === 'object' && node !== null) {
      return Object.values(node).every((item) => visit(item, depth + 1));
    }
    return true;
  }

  visit(value, 1);
});

// Unbounded-structure JSON value (used for Artifact.data), unlike JsonValueSchema
// above which also enforces ActionRequest.value bounds. Only byte size is checked.
const plainJsonValueSchema: z.ZodType<JsonValue> = z.lazy(() => z.union([
  z.string(),
  z.number(),
  z.boolean(),
  z.null(),
  z.array(plainJsonValueSchema),
  z.record(z.string(), plainJsonValueSchema),
]));

function jsonByteSize(value: JsonValue): number {
  return new TextEncoder().encode(JSON.stringify(value ?? null)).length;
}

export const ActionDescriptorSchema = z.strictObject({
  action_id: z.string().min(1).max(128),
  label: labelTextSchema.min(1),
  intent: z.enum(['submit', 'select', 'approve', 'reject', 'open', 'dismiss']).default('submit'),
});

export const ChoiceOptionSchema = z.strictObject({
  label: labelTextSchema.min(1),
  value: z.union([z.string(), z.number(), z.boolean()]),
  description: componentTextSchema.nullable().default(null),
});

export const TabDescriptorSchema = z.strictObject({
  label: labelTextSchema.min(1),
  child_id: componentIdSchema,
});

export const CitationDescriptorSchema = z.strictObject({
  citation_id: z.string().min(1).max(256),
  title: titleTextSchema.min(1),
  source_type: z.string().min(1),
  url: httpsUrlSchema,
  year: z.int().nullable().default(null),
  relevant_passage: componentTextSchema.nullable().default(null),
});

export const GraphSeriesSchema = z.strictObject({
  name: labelTextSchema.min(1),
  values: z.array(z.number()).max(MAX_GRAPH_VALUES),
});

export const PlanStepSchema = z.strictObject({
  step_id: z.string().min(1).max(128),
  title: titleTextSchema.min(1),
  description: componentTextSchema.nullable().default(null),
  status: z.enum(['pending', 'in_progress', 'completed', 'blocked']).default('pending'),
});

export const PlanSchema = z.strictObject({
  summary: summaryTextSchema.min(1),
  steps: z.array(PlanStepSchema).min(1).max(MAX_PLAN_STEPS),
}).superRefine((plan, context) => {
  const stepIds = plan.steps.map((step) => step.step_id);
  if (new Set(stepIds).size !== stepIds.length) {
    context.addIssue({ code: 'custom', message: 'plan step IDs must be unique', path: ['steps'] });
  }
});

const componentBaseShape = { id: componentIdSchema };

export const TextComponentSchema = z.strictObject({
  ...componentBaseShape,
  type: z.literal('Text').default('Text'),
  content: componentTextSchema,
  variant: z.enum(['h1', 'h2', 'h3', 'h4', 'h5', 'caption', 'body']).default('body'),
});

export const ImageComponentSchema = z.strictObject({
  ...componentBaseShape,
  type: z.literal('Image').default('Image'),
  src: httpsUrlSchema,
  alt: componentTextSchema.default(''),
  width: z.int().positive().nullable().default(null),
  height: z.int().positive().nullable().default(null),
});

export const CardComponentSchema = z.strictObject({
  ...componentBaseShape,
  type: z.literal('Card').default('Card'),
  title: titleTextSchema.nullable().default(null),
  body: bodyTextSchema.nullable().default(null),
  children: z.array(componentIdSchema).max(MAX_CHILDREN).default([]),
  actions: z.array(ActionDescriptorSchema).max(MAX_ACTIONS).default([]),
  interaction_id: interactionIdSchema.nullable().default(null),
}).superRefine((component, context) => {
  if (component.actions.length > 0 && component.interaction_id === null) {
    context.addIssue({ code: 'custom', message: 'interactive cards require interaction_id', path: ['interaction_id'] });
  }
});

export const ChoicePickerComponentSchema = z.strictObject({
  ...componentBaseShape,
  type: z.literal('ChoicePicker').default('ChoicePicker'),
  interaction_id: interactionIdSchema,
  label: labelTextSchema,
  options: z.array(ChoiceOptionSchema).min(1).max(MAX_OPTIONS),
  multi: z.boolean().default(false),
  action: ActionDescriptorSchema,
}).superRefine((component, context) => {
  const values = component.options.map((option) => `${typeof option.value}:${JSON.stringify(option.value)}`);
  if (new Set(values).size !== values.length) {
    context.addIssue({ code: 'custom', message: 'choice option values must be unique', path: ['options'] });
  }
});

export const TextFieldComponentSchema = z.strictObject({
  ...componentBaseShape,
  type: z.literal('TextField').default('TextField'),
  interaction_id: interactionIdSchema,
  label: labelTextSchema,
  placeholder: componentTextSchema.default(''),
  multiline: z.boolean().default(false),
  action: ActionDescriptorSchema,
});

export const ButtonComponentSchema = z.strictObject({
  ...componentBaseShape,
  type: z.literal('Button').default('Button'),
  interaction_id: interactionIdSchema,
  label: labelTextSchema,
  action: ActionDescriptorSchema,
});

export const ListComponentSchema = z.strictObject({
  ...componentBaseShape,
  type: z.literal('List').default('List'),
  children: z.array(componentIdSchema).max(MAX_CHILDREN).default([]),
  ordered: z.boolean().default(false),
});

export const RowComponentSchema = z.strictObject({
  ...componentBaseShape,
  type: z.literal('Row').default('Row'),
  children: z.array(componentIdSchema).max(MAX_CHILDREN).default([]),
});

export const ColumnComponentSchema = z.strictObject({
  ...componentBaseShape,
  type: z.literal('Column').default('Column'),
  children: z.array(componentIdSchema).max(MAX_CHILDREN).default([]),
});

export const TabsComponentSchema = z.strictObject({
  ...componentBaseShape,
  type: z.literal('Tabs').default('Tabs'),
  tabs: z.array(TabDescriptorSchema).min(1).max(MAX_TABS),
  active_index: z.int().min(0).default(0),
}).superRefine((component, context) => {
  if (component.active_index >= component.tabs.length) {
    context.addIssue({ code: 'custom', message: 'active_index must identify an existing tab', path: ['active_index'] });
  }
});

export const ModalComponentSchema = z.strictObject({
  ...componentBaseShape,
  type: z.literal('Modal').default('Modal'),
  title: titleTextSchema,
  body: bodyTextSchema.nullable().default(null),
  children: z.array(componentIdSchema).max(MAX_CHILDREN).default([]),
  open: z.boolean().default(true),
  actions: z.array(ActionDescriptorSchema).max(MAX_ACTIONS).default([]),
  interaction_id: interactionIdSchema.nullable().default(null),
}).superRefine((component, context) => {
  if (component.actions.length > 0 && component.interaction_id === null) {
    context.addIssue({ code: 'custom', message: 'interactive modals require interaction_id', path: ['interaction_id'] });
  }
});

export const MarkdownComponentSchema = z.strictObject({
  ...componentBaseShape,
  type: z.literal('Markdown').default('Markdown'),
  content: markdownTextSchema,
});

export const GraphComponentSchema = z.strictObject({
  ...componentBaseShape,
  type: z.literal('Graph').default('Graph'),
  graph_type: z.enum(['line', 'bar-horizontal', 'bar-vertical', 'scatter', 'pie']).default('line'),
  title: titleTextSchema.nullable().default(null),
  categories: z.array(z.union([z.string(), z.number()])).max(MAX_GRAPH_CATEGORIES).default([]),
  series: z.array(GraphSeriesSchema).min(1).max(MAX_GRAPH_SERIES),
  x_label: labelTextSchema.nullable().default(null),
  y_label: labelTextSchema.nullable().default(null),
}).superRefine((component, context) => {
  const totalPoints = component.series.reduce((total, series) => total + series.values.length, 0);
  if (totalPoints > MAX_GRAPH_TOTAL_POINTS) {
    context.addIssue({ code: 'custom', message: `graph total points exceed ${MAX_GRAPH_TOTAL_POINTS}`, path: ['series'] });
  }
});

export const CitationChipComponentSchema = z.strictObject({
  ...componentBaseShape,
  type: z.literal('CitationChip').default('CitationChip'),
  citation: CitationDescriptorSchema,
});

export const ConfidenceBadgeComponentSchema = z.strictObject({
  ...componentBaseShape,
  type: z.literal('ConfidenceBadge').default('ConfidenceBadge'),
  score: z.number().min(0).max(1),
  label: labelTextSchema,
});

export const EvidencePanelComponentSchema = z.strictObject({
  ...componentBaseShape,
  type: z.literal('EvidencePanel').default('EvidencePanel'),
  citations: z.array(CitationDescriptorSchema).max(MAX_CITATIONS),
  summary: summaryTextSchema,
  children: z.array(componentIdSchema).max(MAX_CHILDREN).default([]),
});

export const ReviewGateComponentSchema = z.strictObject({
  ...componentBaseShape,
  type: z.literal('ReviewGate').default('ReviewGate'),
  interaction_id: interactionIdSchema,
  plan: PlanSchema,
  actions: z.array(ActionDescriptorSchema).min(1).max(MAX_ACTIONS),
  require_approval: z.boolean().default(true),
});

export const TableColumnSchema = z.strictObject({
  key: z.string().min(1).max(64),
  label: labelTextSchema.min(1),
  align: z.enum(['left', 'right', 'center']).default('left'),
  kind: z.enum(['text', 'number']).default('text'),
});

const tableCellSchema = z.union([z.string(), z.number(), z.boolean(), z.null()]);
export type TableCell = z.infer<typeof tableCellSchema>;

export const TableCellRefSchema = z.strictObject({
  row: z.int().min(0),
  column: z.int().min(0),
});

export const TableComponentSchema = z.strictObject({
  ...componentBaseShape,
  type: z.literal('Table').default('Table'),
  title: titleTextSchema.nullable().default(null),
  caption: componentTextSchema.nullable().default(null),
  columns: z.array(TableColumnSchema).min(1).max(MAX_TABLE_COLUMNS),
  rows: z.array(z.array(tableCellSchema)).max(MAX_TABLE_ROWS).default([]),
  flagged_cells: z.array(TableCellRefSchema).max(MAX_TABLE_CELLS).default([]),
  sortable: z.boolean().default(false),
}).superRefine((component, context) => {
  const keys = component.columns.map((column) => column.key);
  if (new Set(keys).size !== keys.length) {
    context.addIssue({ code: 'custom', message: 'table column keys must be unique', path: ['columns'] });
  }
  const width = component.columns.length;
  component.rows.forEach((row, index) => {
    if (row.length !== width) {
      context.addIssue({ code: 'custom', message: 'every table row must have one cell per column', path: ['rows', index] });
    }
  });
  if (width * component.rows.length > MAX_TABLE_CELLS) {
    context.addIssue({ code: 'custom', message: `table cells exceed ${MAX_TABLE_CELLS}`, path: ['rows'] });
  }
  component.flagged_cells.forEach((cell, index) => {
    if (cell.row >= component.rows.length || cell.column >= width) {
      context.addIssue({ code: 'custom', message: 'flagged cells must identify existing cells', path: ['flagged_cells', index] });
    }
  });
});

export const MetricSchema = z.strictObject({
  label: labelTextSchema.min(1),
  value: z.string().min(1).max(64),
  numeric_value: z.number().nullable().default(null),
  unit: z.string().max(32).nullable().default(null),
  context: componentTextSchema.nullable().default(null),
  trend: z.enum(['up', 'down', 'flat']).nullable().default(null),
  sentiment: z.enum(['positive', 'negative', 'neutral']).default('neutral'),
  verified: z.boolean().default(true),
});

export const StatGridComponentSchema = z.strictObject({
  ...componentBaseShape,
  type: z.literal('StatGrid').default('StatGrid'),
  title: titleTextSchema.nullable().default(null),
  metrics: z.array(MetricSchema).min(1).max(MAX_METRICS),
});

export const CalloutComponentSchema = z.strictObject({
  ...componentBaseShape,
  type: z.literal('Callout').default('Callout'),
  tone: z.enum(['info', 'success', 'warning', 'danger', 'neutral']).default('info'),
  title: titleTextSchema.nullable().default(null),
  content: markdownTextSchema,
});

export const TimelineItemSchema = z.strictObject({
  item_id: z.string().min(1).max(128),
  date_label: z.string().min(1).max(64),
  title: titleTextSchema.min(1),
  description: componentTextSchema.nullable().default(null),
  status: z.enum(['completed', 'in_progress', 'planned', 'blocked']).nullable().default(null),
});

export const TimelineComponentSchema = z.strictObject({
  ...componentBaseShape,
  type: z.literal('Timeline').default('Timeline'),
  title: titleTextSchema.nullable().default(null),
  items: z.array(TimelineItemSchema).min(1).max(MAX_TIMELINE_ITEMS),
}).superRefine((component, context) => {
  const itemIds = component.items.map((item) => item.item_id);
  if (new Set(itemIds).size !== itemIds.length) {
    context.addIssue({ code: 'custom', message: 'timeline item IDs must be unique', path: ['items'] });
  }
});

export const ComparisonCriterionSchema = z.strictObject({
  label: labelTextSchema.min(1),
  values: z.array(componentTextSchema).max(MAX_COMPARISON_OPTIONS),
  best_index: z.int().min(0).nullable().default(null),
});

export const ComparisonComponentSchema = z.strictObject({
  ...componentBaseShape,
  type: z.literal('Comparison').default('Comparison'),
  title: titleTextSchema.nullable().default(null),
  options: z.array(labelTextSchema.min(1)).min(2).max(MAX_COMPARISON_OPTIONS),
  criteria: z.array(ComparisonCriterionSchema).min(1).max(MAX_COMPARISON_CRITERIA),
  recommendation: componentTextSchema.nullable().default(null),
}).superRefine((component, context) => {
  const width = component.options.length;
  component.criteria.forEach((criterion, index) => {
    if (criterion.values.length !== width) {
      context.addIssue({ code: 'custom', message: 'every comparison criterion must have one value per option', path: ['criteria', index, 'values'] });
    }
    if (criterion.best_index !== null && criterion.best_index >= width) {
      context.addIssue({ code: 'custom', message: 'best_index must identify an existing option', path: ['criteria', index, 'best_index'] });
    }
  });
});

export const ArtifactComponentSchema = z.strictObject({
  ...componentBaseShape,
  type: z.literal('Artifact').default('Artifact'),
  title: titleTextSchema.min(1).max(200),
  description: componentTextSchema.min(1),
  html: z.string().min(1).max(MAX_ARTIFACT_HTML_CHARS),
  data: plainJsonValueSchema.nullable().default(null),
  height: z.int().min(ARTIFACT_MIN_HEIGHT).max(ARTIFACT_MAX_HEIGHT).default(ARTIFACT_DEFAULT_HEIGHT),
}).superRefine((component, context) => {
  const deniedPatterns = findDeniedArtifactHtmlPatterns(component.html);
  if (deniedPatterns.length > 0) {
    context.addIssue({ code: 'custom', message: `artifact html matches a denied pattern: ${deniedPatterns[0]}`, path: ['html'] });
  }
  if (jsonByteSize(component.data) > MAX_ARTIFACT_DATA_BYTES) {
    context.addIssue({ code: 'custom', message: `artifact data exceeds ${MAX_ARTIFACT_DATA_BYTES} bytes`, path: ['data'] });
  }
});

export const ComponentSchema = z.discriminatedUnion('type', [
  TextComponentSchema,
  ImageComponentSchema,
  CardComponentSchema,
  ChoicePickerComponentSchema,
  TextFieldComponentSchema,
  ButtonComponentSchema,
  ListComponentSchema,
  RowComponentSchema,
  ColumnComponentSchema,
  TabsComponentSchema,
  ModalComponentSchema,
  MarkdownComponentSchema,
  GraphComponentSchema,
  CitationChipComponentSchema,
  ConfidenceBadgeComponentSchema,
  EvidencePanelComponentSchema,
  ReviewGateComponentSchema,
  TableComponentSchema,
  StatGridComponentSchema,
  CalloutComponentSchema,
  TimelineComponentSchema,
  ComparisonComponentSchema,
  ArtifactComponentSchema,
]);

export const CEIUISurfaceSchema = z.strictObject({
  content_type: z.literal(CEI_UI_CONTENT_TYPE).default(CEI_UI_CONTENT_TYPE),
  protocol: z.literal(CEI_UI_PROTOCOL).default(CEI_UI_PROTOCOL),
  version: z.enum(CEI_UI_SUPPORTED_VERSIONS).default(CEI_UI_VERSION),
  surface_id: z.string().min(1).max(128),
  revision: z.int().positive().default(1),
  root_id: componentIdSchema,
  components: z.array(ComponentSchema).min(1).max(MAX_COMPONENTS),
}).superRefine((surface, context) => {
  const componentsById = new Map(surface.components.map((component) => [component.id, component]));
  if (componentsById.size !== surface.components.length) {
    context.addIssue({ code: 'custom', message: 'component IDs must be unique', path: ['components'] });
  }
  if (!componentsById.has(surface.root_id)) {
    context.addIssue({ code: 'custom', message: 'root_id must identify an existing component', path: ['root_id'] });
  }

  const artifacts = surface.components.filter((component) => component.type === 'Artifact');
  if (artifacts.length > MAX_ARTIFACTS_PER_SURFACE) {
    context.addIssue({ code: 'custom', message: `artifacts exceed ${MAX_ARTIFACTS_PER_SURFACE} per surface`, path: ['components'] });
  }
  const artifactBytes = artifacts.reduce(
    (total, artifact) => total + new TextEncoder().encode(artifact.html).length + jsonByteSize(artifact.data),
    0,
  );
  if (artifactBytes > MAX_ARTIFACT_TOTAL_BYTES) {
    context.addIssue({ code: 'custom', message: `artifact payload exceeds ${MAX_ARTIFACT_TOTAL_BYTES} bytes`, path: ['components'] });
  }

  const references = new Map(surface.components.map((component) => [component.id, childReferences(component)]));
  const missing = new Set<string>();
  for (const childIds of references.values()) {
    for (const childId of childIds) {
      if (!componentsById.has(childId)) missing.add(childId);
    }
  }
  if (missing.size > 0) {
    context.addIssue({
      code: 'custom',
      message: `child references do not exist: ${JSON.stringify([...missing].sort())}`,
      path: ['components'],
    });
    return;
  }

  const visited = new Set<string>();
  const active = new Set<string>();
  let graphError: string | undefined;
  function visit(componentId: string, depth: number) {
    if (graphError || visited.has(componentId)) return;
    if (depth > MAX_COMPONENT_DEPTH) {
      graphError = `component depth exceeds ${MAX_COMPONENT_DEPTH}`;
      return;
    }
    if (active.has(componentId)) {
      graphError = 'component graph must not contain cycles';
      return;
    }
    active.add(componentId);
    for (const childId of references.get(componentId) ?? []) visit(childId, depth + 1);
    active.delete(componentId);
    visited.add(componentId);
  }
  for (const componentId of componentsById.keys()) visit(componentId, 1);
  if (graphError) context.addIssue({ code: 'custom', message: graphError, path: ['components'] });
});

export const ActionRequestSchema = z.strictObject({
  request_id: z.string().min(1).max(128),
  session_id: z.string().min(1).max(256),
  surface_id: z.string().min(1).max(128),
  revision: z.int().positive(),
  interaction_id: interactionIdSchema,
  component_id: componentIdSchema,
  action_id: z.string().min(1).max(128),
  value: JsonValueSchema.default(null),
});

export const ActionResponseSchema = z.strictObject({
  ...ActionRequestSchema.shape,
  status: z.enum(['accepted', 'completed', 'stale', 'rejected', 'error']),
  message: z.string().nullable().default(null),
});

export type ActionDescriptor = z.infer<typeof ActionDescriptorSchema>;
export type ChoiceOption = z.infer<typeof ChoiceOptionSchema>;
export type CitationDescriptor = z.infer<typeof CitationDescriptorSchema>;
export type GraphSeries = z.infer<typeof GraphSeriesSchema>;
export type Plan = z.infer<typeof PlanSchema>;
export type TextComponent = z.infer<typeof TextComponentSchema>;
export type ImageComponent = z.infer<typeof ImageComponentSchema>;
export type CardComponent = z.infer<typeof CardComponentSchema>;
export type ChoicePickerComponent = z.infer<typeof ChoicePickerComponentSchema>;
export type TextFieldComponent = z.infer<typeof TextFieldComponentSchema>;
export type ButtonComponent = z.infer<typeof ButtonComponentSchema>;
export type ListComponent = z.infer<typeof ListComponentSchema>;
export type RowComponent = z.infer<typeof RowComponentSchema>;
export type ColumnComponent = z.infer<typeof ColumnComponentSchema>;
export type TabsComponent = z.infer<typeof TabsComponentSchema>;
export type ModalComponent = z.infer<typeof ModalComponentSchema>;
export type MarkdownComponent = z.infer<typeof MarkdownComponentSchema>;
export type GraphComponent = z.infer<typeof GraphComponentSchema>;
export type CitationChipComponent = z.infer<typeof CitationChipComponentSchema>;
export type ConfidenceBadgeComponent = z.infer<typeof ConfidenceBadgeComponentSchema>;
export type EvidencePanelComponent = z.infer<typeof EvidencePanelComponentSchema>;
export type ReviewGateComponent = z.infer<typeof ReviewGateComponentSchema>;
export type TableColumn = z.infer<typeof TableColumnSchema>;
export type TableCellRef = z.infer<typeof TableCellRefSchema>;
export type TableComponent = z.infer<typeof TableComponentSchema>;
export type Metric = z.infer<typeof MetricSchema>;
export type StatGridComponent = z.infer<typeof StatGridComponentSchema>;
export type CalloutComponent = z.infer<typeof CalloutComponentSchema>;
export type TimelineItem = z.infer<typeof TimelineItemSchema>;
export type TimelineComponent = z.infer<typeof TimelineComponentSchema>;
export type ComparisonCriterion = z.infer<typeof ComparisonCriterionSchema>;
export type ComparisonComponent = z.infer<typeof ComparisonComponentSchema>;
export type ArtifactComponent = z.infer<typeof ArtifactComponentSchema>;
export type CEIUIComponent = z.infer<typeof ComponentSchema>;
export type CEIUISurface = z.infer<typeof CEIUISurfaceSchema>;
export type ActionRequest = z.infer<typeof ActionRequestSchema>;
export type ActionResponse = z.infer<typeof ActionResponseSchema>;

export function parseCEIUISurface(value: unknown): CEIUISurface {
  return CEIUISurfaceSchema.parse(value);
}

export function safeParseCEIUISurface(value: unknown) {
  return CEIUISurfaceSchema.safeParse(value);
}

function childReferences(component: CEIUIComponent): string[] {
  switch (component.type) {
    case 'Tabs':
      return component.tabs.map((tab) => tab.child_id);
    case 'Card':
    case 'List':
    case 'Row':
    case 'Column':
    case 'Modal':
    case 'EvidencePanel':
      return component.children;
    default:
      return [];
  }
}