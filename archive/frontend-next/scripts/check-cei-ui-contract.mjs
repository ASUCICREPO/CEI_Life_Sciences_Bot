import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { actionResponseMatchesRequest } from '../lib/cei-ui/action-correlation.ts';
import {
  ActionRequestSchema,
  ActionResponseSchema,
  ARTIFACT_DEFAULT_HEIGHT,
  ARTIFACT_HTML_DENYLIST,
  ARTIFACT_MAX_HEIGHT,
  ARTIFACT_MIN_HEIGHT,
  ArtifactComponentSchema,
  CEIUISurfaceSchema,
  GraphComponentSchema,
  MAX_ACTION_VALUE_ARRAY_ENTRIES,
  MAX_ACTION_VALUE_DEPTH,
  MAX_ACTION_VALUE_KEY_LENGTH,
  MAX_ACTION_VALUE_MAPPING_ENTRIES,
  MAX_ACTION_VALUE_NODES,
  MAX_ACTION_VALUE_STRING_LENGTH,
  MAX_ACTIONS,
  MAX_ARTIFACT_DATA_BYTES,
  MAX_ARTIFACT_HTML_CHARS,
  MAX_ARTIFACT_TOTAL_BYTES,
  MAX_ARTIFACTS_PER_SURFACE,
  MAX_CHILDREN,
  MAX_CITATIONS,
  MAX_COMPARISON_CRITERIA,
  MAX_COMPARISON_OPTIONS,
  MAX_COMPONENT_DEPTH,
  MAX_COMPONENTS,
  MAX_GRAPH_CATEGORIES,
  MAX_GRAPH_SERIES,
  MAX_GRAPH_TOTAL_POINTS,
  MAX_GRAPH_VALUES,
  MAX_METRICS,
  MAX_OPTIONS,
  MAX_PLAN_STEPS,
  MAX_TABLE_CELLS,
  MAX_TABLE_COLUMNS,
  MAX_TABLE_ROWS,
  MAX_TABS,
  MAX_TIMELINE_ITEMS,
  TableComponentSchema,
} from '../lib/cei-ui/schema.ts';

const expectedLimits = {
  MAX_COMPONENTS: 250,
  MAX_COMPONENT_DEPTH: 32,
  MAX_CHILDREN: 250,
  MAX_ACTIONS: 20,
  MAX_TABS: 20,
  MAX_OPTIONS: 100,
  MAX_CITATIONS: 100,
  MAX_PLAN_STEPS: 100,
  MAX_GRAPH_SERIES: 20,
  MAX_GRAPH_CATEGORIES: 1_000,
  MAX_GRAPH_VALUES: 1_000,
  MAX_GRAPH_TOTAL_POINTS: 5_000,
  MAX_ACTION_VALUE_DEPTH: 8,
  MAX_ACTION_VALUE_NODES: 1_000,
  MAX_ACTION_VALUE_MAPPING_ENTRIES: 100,
  MAX_ACTION_VALUE_ARRAY_ENTRIES: 100,
  MAX_ACTION_VALUE_KEY_LENGTH: 128,
  MAX_ACTION_VALUE_STRING_LENGTH: 10_000,
  MAX_TABLE_COLUMNS: 12,
  MAX_TABLE_ROWS: 200,
  MAX_TABLE_CELLS: 2_000,
  MAX_METRICS: 12,
  MAX_TIMELINE_ITEMS: 50,
  MAX_COMPARISON_OPTIONS: 6,
  MAX_COMPARISON_CRITERIA: 30,
  MAX_ARTIFACT_HTML_CHARS: 65_536,
  MAX_ARTIFACT_DATA_BYTES: 65_536,
  MAX_ARTIFACTS_PER_SURFACE: 4,
  MAX_ARTIFACT_TOTAL_BYTES: 262_144,
  ARTIFACT_MIN_HEIGHT: 120,
  ARTIFACT_MAX_HEIGHT: 1_200,
  ARTIFACT_DEFAULT_HEIGHT: 420,
};

assert.deepEqual({
  MAX_COMPONENTS,
  MAX_COMPONENT_DEPTH,
  MAX_CHILDREN,
  MAX_ACTIONS,
  MAX_TABS,
  MAX_OPTIONS,
  MAX_CITATIONS,
  MAX_PLAN_STEPS,
  MAX_GRAPH_SERIES,
  MAX_GRAPH_CATEGORIES,
  MAX_GRAPH_VALUES,
  MAX_GRAPH_TOTAL_POINTS,
  MAX_ACTION_VALUE_DEPTH,
  MAX_ACTION_VALUE_NODES,
  MAX_ACTION_VALUE_MAPPING_ENTRIES,
  MAX_ACTION_VALUE_ARRAY_ENTRIES,
  MAX_ACTION_VALUE_KEY_LENGTH,
  MAX_ACTION_VALUE_STRING_LENGTH,
  MAX_TABLE_COLUMNS,
  MAX_TABLE_ROWS,
  MAX_TABLE_CELLS,
  MAX_METRICS,
  MAX_TIMELINE_ITEMS,
  MAX_COMPARISON_OPTIONS,
  MAX_COMPARISON_CRITERIA,
  MAX_ARTIFACT_HTML_CHARS,
  MAX_ARTIFACT_DATA_BYTES,
  MAX_ARTIFACTS_PER_SURFACE,
  MAX_ARTIFACT_TOTAL_BYTES,
  ARTIFACT_MIN_HEIGHT,
  ARTIFACT_MAX_HEIGHT,
  ARTIFACT_DEFAULT_HEIGHT,
}, expectedLimits);

const actionRequest = {
  request_id: 'request',
  session_id: 'session',
  surface_id: 'surface',
  revision: 1,
  interaction_id: 'interaction',
  component_id: 'component',
  action_id: 'action',
};

let depthEight = null;
for (let index = 0; index < 7; index += 1) depthEight = { value: depthEight };
assert.equal(ActionRequestSchema.safeParse({ ...actionRequest, value: depthEight }).success, true);
assert.equal(ActionRequestSchema.safeParse({ ...actionRequest, value: { value: depthEight } }).success, false);
assert.equal(ActionRequestSchema.safeParse({ ...actionRequest, value: Array(101).fill(null) }).success, false);
assert.equal(ActionRequestSchema.safeParse({
  ...actionRequest,
  value: Object.fromEntries(Array.from({ length: 101 }, (_, index) => [`key-${index}`, null])),
}).success, false);
assert.equal(ActionRequestSchema.safeParse({ ...actionRequest, value: { ['k'.repeat(129)]: null } }).success, false);
assert.equal(ActionRequestSchema.safeParse({ ...actionRequest, value: 'v'.repeat(10_001) }).success, false);
assert.equal(ActionRequestSchema.safeParse({
  ...actionRequest,
  value: Array.from({ length: 100 }, () => Array(9).fill(null)),
}).success, false);

const graph = {
  id: 'graph',
  type: 'Graph',
  series: Array.from({ length: 5 }, (_, index) => ({ name: `Series ${index}`, values: Array(1_000).fill(1) })),
};
assert.equal(GraphComponentSchema.safeParse(graph).success, true);
assert.equal(GraphComponentSchema.safeParse({
  ...graph,
  series: [...graph.series, { name: 'Over limit', values: [1] }],
}).success, false);

const actionResponse = { ...actionRequest, value: null, status: 'completed' };
assert.equal(ActionResponseSchema.safeParse({
  ...actionResponse,
  message: 'm'.repeat(2_001),
}).success, true);

const correlatedRequest = ActionRequestSchema.parse({ ...actionRequest, value: { nested: [1, 'two'] } });
const correlatedResponse = ActionResponseSchema.parse({ ...correlatedRequest, status: 'completed' });
assert.equal(actionResponseMatchesRequest(correlatedRequest, correlatedResponse), true);
for (const field of [
  'request_id',
  'session_id',
  'surface_id',
  'revision',
  'interaction_id',
  'component_id',
  'action_id',
  'value',
]) {
  const mismatch = field === 'revision' ? correlatedResponse.revision + 1 :
    field === 'value' ? { nested: [1, 'different'] } : `${correlatedResponse[field]}-different`;
  assert.equal(actionResponseMatchesRequest(correlatedRequest, { ...correlatedResponse, [field]: mismatch }), false);
}

// --- CEI UI 1.1: golden fixture ---
const fixturePath = fileURLToPath(new URL('./fixtures/cei-ui-1.1-sample.json', import.meta.url));
const fixture = JSON.parse(readFileSync(fixturePath, 'utf-8'));
const fixtureResult = CEIUISurfaceSchema.safeParse(fixture);
assert.equal(fixtureResult.success, true, fixtureResult.success ? '' : JSON.stringify(fixtureResult.error?.issues));

// --- CEI UI 1.1: version compatibility ---
const surface10 = {
  content_type: 'application/vnd.cei.ui+json',
  protocol: 'cei-ui',
  version: '1.0',
  surface_id: 'legacy',
  revision: 1,
  root_id: 'root',
  components: [{ type: 'Text', id: 'root', content: 'Legacy 1.0 surface' }],
};
assert.equal(CEIUISurfaceSchema.safeParse(surface10).success, true);

// --- Table: ragged rows rejected ---
const validTable = {
  id: 'table',
  type: 'Table',
  columns: [{ key: 'c0', label: 'A' }, { key: 'c1', label: 'B' }],
  rows: [['x', 1], ['y', 2]],
};
assert.equal(TableComponentSchema.safeParse(validTable).success, true);
assert.equal(TableComponentSchema.safeParse({
  ...validTable,
  rows: [['x', 1], ['y']],
}).success, false);

// --- Artifact: denylist rejects malicious html, accepts benign html ---
const artifactBase = {
  id: 'artifact',
  type: 'Artifact',
  title: 'Widget',
  description: 'A benign widget.',
  html: '<div id="out"></div>',
};

const deniedHtmlSnippets = [
  'fetch(',
  'https:',
  '<iframe>',
  'top.location',
  'localStorage',
  'postMessage',
];
for (const snippet of deniedHtmlSnippets) {
  assert.equal(
    ArtifactComponentSchema.safeParse({ ...artifactBase, html: `<div>${snippet}</div>` }).success,
    false,
    `expected artifact html containing "${snippet}" to be rejected`,
  );
}

const benignHtmlSnippets = [
  '<svg viewBox="0 0 10 10"><circle cx="5" cy="5" r="4"></circle></svg>',
  '<canvas id="c"></canvas><script>document.getElementById("c").getContext("2d");</script>',
  '<button id="b">Go</button><script>document.getElementById("b").addEventListener("click", function () {});</script>',
];
for (const html of benignHtmlSnippets) {
  assert.equal(
    ArtifactComponentSchema.safeParse({ ...artifactBase, html }).success,
    true,
    `expected benign artifact html to be accepted: ${html}`,
  );
}
assert.deepEqual([...ARTIFACT_HTML_DENYLIST].length > 0, true);

// --- Surface: more than MAX_ARTIFACTS_PER_SURFACE artifacts rejected ---
function artifactComponent(id) {
  return { id, type: 'Artifact', title: 'Widget', description: 'A benign widget.', html: '<div>ok</div>' };
}
const manyArtifacts = Array.from({ length: MAX_ARTIFACTS_PER_SURFACE + 1 }, (_, index) => artifactComponent(`artifact-${index}`));
const tooManyArtifactsSurface = {
  content_type: 'application/vnd.cei.ui+json',
  protocol: 'cei-ui',
  version: '1.1',
  surface_id: 'too-many-artifacts',
  revision: 1,
  root_id: 'root',
  components: [{ type: 'Column', id: 'root', children: manyArtifacts.map((artifact) => artifact.id) }, ...manyArtifacts],
};
assert.equal(CEIUISurfaceSchema.safeParse(tooManyArtifactsSurface).success, false);
const okArtifactsSurface = {
  ...tooManyArtifactsSurface,
  surface_id: 'ok-artifacts',
  components: tooManyArtifactsSurface.components.map((component) => component.type === 'Column'
    ? { ...component, children: component.children.slice(0, MAX_ARTIFACTS_PER_SURFACE) }
    : component).filter((component) => component.type !== 'Artifact' || manyArtifacts.slice(0, MAX_ARTIFACTS_PER_SURFACE).some((artifact) => artifact.id === component.id)),
};
assert.equal(CEIUISurfaceSchema.safeParse(okArtifactsSurface).success, true);

console.log('CEI UI contract checks passed.');