# CEI UI Protocol 1.1

CEI UI is the implemented protocol for rich agent responses. It uses a small component catalog and deterministic presenters. The 1.1 contract describes completed JSON responses. The streaming route and the pause and resume flow are described in "Transport Status" and "CEI UI 1.2 progressive streaming" below.

Version 1.1 adds six components (`Table`, `StatGrid`, `Callout`, `Timeline`, `Comparison`, `Artifact`) so research and drafting answers can look like a rich document instead of one block of prose. 1.1 is additive: every 1.0 surface stays valid, and no 1.0 field or component changed meaning.

## Protocol Identity

| Item | Value |
|---|---|
| Content type | `application/vnd.cei.ui+json` |
| Protocol | `cei-ui` |
| Version emitted by servers | `1.1` |
| Versions accepted by clients and stored history | `1.0`, `1.1` |
| Agent response field | `ui` |

An agent response also contains a plain-text `text` field.

## Version Rules

- Servers (presenters) always emit `version: "1.1"` on new surfaces.
- Clients, the local action proxy, and stored chat history must accept both `"1.0"` and `"1.1"`. A validator must not reject a surface just because it says `"1.0"` (`shared/persistence/chat_history.py` stores older surfaces at `"1.0"`).
- 1.1 is additive. It adds the six components below and their limits. It does not remove, rename, or change the meaning of any 1.0 component, field, or limit. A 1.0 surface (no 1.1-only components) validates unchanged under the 1.1 schema.
- The `version` string is informational metadata about which presenter produced the surface. The component allowlist below applies to both versions combined; it is not gated by the `version` field itself.

## Text Fallback

When the `ui` object fails schema validation, the client drops `ui` and renders `text` instead, with a visible notice that the rich view is unavailable. The local agent proxy applies the same rule server-side: when the upstream `ui` fails validation, the proxy omits `ui` from the forwarded response and sets a `ui_error` field describing why, instead of failing the whole request.

## Completed Surface

Version 1.1 sends one complete surface. The `ui` object has these fields:

| Field | Type | Rule |
|---|---|---|
| `content_type` | string | Must be `application/vnd.cei.ui+json`. |
| `protocol` | string | Must be `cei-ui`. |
| `version` | string | Must be `1.0` or `1.1`. Servers emit `1.1`. |
| `surface_id` | string | Contains 1 to 128 characters. |
| `revision` | integer | Must be positive. |
| `root_id` | string | Identifies an existing component. |
| `components` | array | Contains 1 to 250 component records. |

The component graph is a flat list. Component fields are direct fields. There is no `props` wrapper.

```json
{
  "id": "answer",
  "type": "Text",
  "content": "The review is complete.",
  "variant": "body"
}
```

Component IDs must be unique. All child references must exist. The graph must not contain cycles. A path must not exceed 32 components. The current validator permits components that are not reachable from `root_id`.

## Component Allowlist

The catalog is the union of the 1.0 and 1.1 component names. The client rejects unknown component types and unknown fields. Catalog changes require a protocol version change.

**1.0 components:**

- `Text`
- `Image`
- `Card`
- `ChoicePicker`
- `TextField`
- `Button`
- `List`
- `Row`
- `Column`
- `Tabs`
- `Modal`
- `Markdown`
- `Graph`
- `CitationChip`
- `ConfidenceBadge`
- `EvidencePanel`
- `ReviewGate`

**New in 1.1:**

- `Table`
- `StatGrid`
- `Callout`
- `Timeline`
- `Comparison`
- `Artifact`

None of the new components accept children; they are leaf nodes in the component graph.

### New Component Limits

| Component | Main fields | Limits and checks |
|---|---|---|
| `Table` | `columns` (key, label, align, kind), `rows`, `flagged_cells`, `sortable` | ≤12 columns, ≤200 rows, ≤2,000 cells; column keys unique; every row has one cell per column; flagged cells must identify existing cells |
| `StatGrid` | `metrics` (label, value, numeric_value, unit, context, trend, sentiment, verified) | ≤12 metrics |
| `Callout` | `tone` (`info`, `success`, `warning`, `danger`, `neutral`), `title`, markdown `content` | — |
| `Timeline` | `items` (item_id, date_label, title, description, status) | ≤50 items, unique `item_id` values |
| `Comparison` | `options`, `criteria` (values, best_index), `recommendation` | 2–6 options, ≤30 criteria; every criterion has one value per option; `best_index` must identify an existing option |
| `Artifact` | `title`, `description`, `html`, JSON `data`, `height` | `html` ≤64 KiB, `data` ≤64 KiB, `height` 120–1,200 (default 420); `html` must pass the artifact denylist lint |

Surface-level artifact limits: at most 4 `Artifact` components per surface, and at most 262,144 bytes of combined `html` + `data` across all artifacts on one surface.

## Deterministic Presentation

Models never emit CEI UI component records, and they never emit arbitrary UI, HTML, CSS, JavaScript, event handlers, or component definitions. The research and drafting agents instead call a structured tool and return a typed `ResponseDocument` (`shared/schemas/response_blocks.py`): a title, a summary, up to four key metrics, one to twelve sections of one to ten typed blocks (`prose`, `table`, `metrics`, `chart`, `comparison`, `callout`, `timeline`, `artifact`), a confidence rating, and open questions. Every non-prose block carries `basis` (`"sourced"` or `"estimate"`) and `source_ids`; the server, not the model, sets `verification` after grounding (see "Grounding of numeric content" below).

Deterministic presenters (`shared/presenters/cei_ui.py`) map each block to one or more allowlisted CEI UI components, assign deterministic component IDs (`s{section}-b{block}-{kind}`), build the flat, cycle-free component graph, and enforce the component budget for the surface. Models never construct `Row`, `Column`, `Tabs`, or `Card` structure, and never choose component IDs.

The only model-authored content that ever executes is the `html` field of an artifact (`ArtifactBlock` on the model side, `Artifact` component on the wire), and it executes only inside the sandboxed iframe described in "Artifact Security" below. Every other field a model can influence — prose text, numbers, table cells, metric labels, markdown strings — is data that fixed application code renders; the model cannot affect how that data is displayed.

If a `ResponseDocument` fails to parse, or presentation of it raises a `ValidationError`, the presenter falls back to the legacy text-derived surface (one `Markdown` component per `##`-delimited section) rather than failing the response.

## Grounding of numeric content

After the model returns a `ResponseDocument`, `shared/grounding.py` builds a normalized evidence corpus from the query, the retrieved results, and the citation titles and passages: lowercase, thousands separators removed, whitespace collapsed.

`is_grounded` matches a number only at a number boundary (`(?<![\d.])X(?!\d)`), trying the value as `%g`, as an integer, and rounded to 1 and 2 decimal places. This is a boundary match, so `1` does not match inside `2019`.

Policy applied before the presenter builds the surface:

- A sourced (`basis="sourced"`) chart with any ungrounded value is dropped entirely; a trace note records why.
- An estimate (`basis="estimate"`) chart is kept, with a caption noting it is an estimate.
- An ungrounded metric is marked `verification="unverified"` and is excluded from `key_metrics`.
- Ungrounded table cells are recorded in `unverified_cells` and rendered in the `Table` component with a `†` marker.
- Artifact `data` (the optional table backing an artifact) is grounded the same way as a table; when it is not grounded, the presenter adds a warning `Callout` next to the artifact.

Grounding runs once, server-side, immediately after the model call and before the presenter builds the surface. `verification` is hidden from the model-facing tool schema (`SkipJsonSchema`), so the model can neither see nor set it.

## Artifact Security

An `Artifact` component's `html` is the only model-authored code that ever executes in the product. It runs in an isolated iframe (`ArtifactFrame`), never inline in the page.

**Sandbox.** The iframe carries `sandbox="allow-scripts"` only. No other sandbox token is present — in particular, `allow-same-origin`, `allow-popups`, `allow-forms`, `allow-modals`, `allow-top-navigation` (and its `-by-user-activation` variant), `allow-downloads`, `allow-pointer-lock`, `allow-presentation`, `allow-orientation-lock`, `allow-popups-to-escape-sandbox`, and `allow-storage-access-by-user-activation` are all omitted. Because `allow-same-origin` is omitted, the frame executes with an opaque (`null`) origin and cannot read cookies, storage, or anything else from the parent origin.

**Content.** The host, not the model, builds the `srcDoc` string. The frame is loaded with `referrerPolicy="no-referrer"` and an empty `allow` attribute (all Permissions Policy features denied). The `srcDoc` contains, in order:

1. A CSP `<meta>` tag:
   ```
   default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:; connect-src 'none'; media-src 'none'; object-src 'none'; frame-src 'none'; child-src 'none'; worker-src 'none'; form-action 'none'; base-uri 'none';
   ```
2. The artifact's `data`, injected as an escaped `<script type="application/json" id="cei-data">` element (never interpolated into the HTML or JS directly).
3. The model-authored `html` fragment.
4. A small host-written resize reporter script.

**Resize channel.** The frame can send exactly one message type to the host: `{"type": "cei-artifact-resize", "height": <number>}`. The parent accepts a message only when `event.source` is the artifact iframe's `contentWindow`, `event.origin === "null"` (the origin a sandboxed, non-`allow-same-origin` frame reports), and the payload has exactly the keys `type` and `height`. The accepted height is clamped to 120–1,200 pixels and throttled to at most one update per animation frame.

**Navigation tripwire.** The host attaches its `onLoad` handler once for the initial `srcDoc` load. A second `onLoad` firing on the same iframe means the frame navigated away from its sandboxed document. The host treats that as a break-out attempt: it unmounts the frame immediately and shows "Interactive view was blocked" instead.

**Defense in depth, not the boundary.** `ARTIFACT_HTML_DENYLIST` (`shared/schemas/cei_ui.py`) and its TypeScript mirror (`lib/cei-ui/schema.ts`) reject `html` containing obvious network, navigation, storage, or frame-escape patterns (`fetch(`, `XMLHttpRequest`, `location =`, `postMessage`, `localStorage`, `<iframe`, `javascript:`, and similar) before a surface is ever produced or accepted. This lint is a second layer, run server-side and client-side. It is not the security boundary — the `sandbox` attribute, the empty `allow` attribute, and the CSP `<meta>` tag are the boundary. The denylist can miss novel constructions; the sandbox must hold even if it does.

**Kill switch.** `NEXT_PUBLIC_CEI_ENABLE_ARTIFACTS` gates rendering on the client. When it is unset or not `"true"`, the frontend never mounts an `ArtifactFrame` and never executes any artifact `html`; it shows only the artifact's `description` and its `data` table.

**Residual risks:**

- **Self-navigation exfiltration.** Because `allow-same-origin` is omitted, the frame has an opaque origin, but sandboxed scripts can still attempt to navigate the frame itself (for example by assigning `location`). A small amount of artifact-local data (such as a query string) could be attached to that one outgoing navigation before the tripwire's second `onLoad` fires and unmounts the frame. The denylist rejects the common `location` patterns, but this is defense in depth, not a guarantee against every construction.
- **CSP inheritance.** `srcDoc` documents inherit the parent document's CSP in addition to their own `<meta>` CSP. The current local preview sets no restrictive top-level CSP, so this is not yet exercised. A future production CloudFront/CDN CSP header must deliberately permit what the artifact's own CSP allows (inline script/style, `data:` images) rather than silently conflicting with or weakening it. This is an open dependency for the deployment work, not something this protocol version solves.
- **`frame-ancestors` is out of scope for the meta tag.** The `<meta http-equiv="Content-Security-Policy">` element cannot enforce `frame-ancestors`. Whether the application itself may be framed is a separate, top-level concern (HTTP CSP header or `X-Frame-Options`), unrelated to the artifact sandbox.
- **No chart library.** By decision, no chart or visualization library is vendored into the artifact sandbox. Artifacts are vanilla JS, SVG, or canvas only, hand-written per response. This keeps the trusted dependency surface at zero but means artifacts cannot rely on a shared, audited rendering library.

## Table/StatGrid Example

```json
{
  "type": "StatGrid",
  "id": "header-metrics",
  "metrics": [
    {"label": "Mean weight loss", "value": "14.9", "numeric_value": 14.9, "unit": "%", "trend": "down", "sentiment": "positive", "verified": true},
    {"label": "Participants", "value": "1,961", "numeric_value": 1961, "verified": false}
  ]
}
```

```json
{
  "type": "Table",
  "id": "s0-b0-table",
  "title": "Weight loss by agent",
  "caption": "Values marked † were not found in retrieved evidence.",
  "columns": [
    {"key": "c0", "label": "Agent", "align": "left", "kind": "text"},
    {"key": "c1", "label": "Weight loss (%)", "align": "right", "kind": "number"}
  ],
  "rows": [["Semaglutide", 14.9], ["Liraglutide", 8.0]],
  "flagged_cells": [{"row": 1, "column": 1}],
  "sortable": true
}
```

An unverified `StatGrid` metric (`verified: false`) and a `flagged_cells` entry on a `Table` both mean the same thing: the server could not confirm that value against the evidence corpus. See "Grounding of numeric content" above.

## Actions

An action descriptor has `action_id`, `label`, and `intent`. The allowed `intent` values are `submit`, `select`, `approve`, `reject`, `open`, and `dismiss`.

```json
{
  "action_id": "approve-plan",
  "label": "Approve",
  "intent": "approve"
}
```

The client sends an action as a JSON POST request. The production route will be set by the API deployment.

| Field | Type | Rule |
|---|---|---|
| `request_id` | string | Contains 1 to 128 characters. |
| `session_id` | string | Contains 1 to 256 characters. |
| `surface_id` | string | Contains 1 to 128 characters. |
| `revision` | integer | Must be positive. |
| `interaction_id` | string | Contains 1 to 128 characters. |
| `component_id` | string | Contains 1 to 128 characters. |
| `action_id` | string | Contains 1 to 128 characters. |
| `value` | JSON value | Defaults to `null` and uses the limits below. |

An action response contains the same fields and adds `status` and an optional `message`. The allowed status values are `accepted`, `completed`, `stale`, `rejected`, and `error`.

Visible CEI UI content has no protocol character cap. This includes component text, Markdown, body, summary, title, label, and an action response `message`. Clients must still render visible content safely, including safe parsing and presentation that prevents resource exhaustion or unsafe executable content.

The local action proxy treats the request fields as an immutable correlation set. It accepts a response only when `request_id`, `session_id`, `surface_id`, `revision`, `interaction_id`, `component_id`, `action_id`, and the complete `value` exactly match the request. A mismatch returns a gateway error and is not forwarded as a valid action response.

### Action States

An action response has one of these states:

| State | Meaning |
|---|---|
| `accepted` | The request is valid and work has started. |
| `completed` | The work is complete. The response can contain a replacement `ui` surface. |
| `stale` | The surface revision is no longer current. The client must get the current surface. |
| `rejected` | The request is not allowed or does not match the action schema. |
| `error` | The server could not process a valid request. A retry can be safe when the server says it is safe. |

## Implemented Structural Limits

The Python and TypeScript validators use strict objects and reject unknown fields. They enforce these limits:

| Data | Limit |
|---|---|
| Components per surface | 250 |
| Component graph depth | 32 |
| Component children | 250 |
| Actions per component | 20 |
| Tabs | 20 |
| Choice options | 100 |
| Citations | 100 |
| Plan steps | 100 |
| Graph series | 20 |
| Graph categories | 1,000 |
| Values per graph series | 1,000 |
| Total graph points | 5,000 |
| Table columns | 12 |
| Table rows | 200 |
| Table cells | 2,000 |
| StatGrid metrics | 12 |
| Timeline items | 50 |
| Comparison options | 2–6 |
| Comparison criteria | 30 |
| Artifact `html` | 65,536 characters |
| Artifact `data` | 65,536 bytes |
| Artifact height | 120–1,200 (default 420) |
| Artifacts per surface | 4 |
| Combined artifact bytes per surface | 262,144 |

Image and citation URLs must use HTTPS and must include a host. Confidence scores must be from 0 through 1. Choice values are strings, numbers, or booleans. Plan step IDs and choice option values must be unique in their local lists.

### Action Value Limits

An action `value` can contain JSON scalars, arrays, and objects. It has these bounds:

| Data | Limit |
|---|---|
| Nesting depth | 8 |
| Total JSON nodes | 1,000 |
| Entries per object | 100 |
| Items per array | 100 |
| Object key | 128 characters |
| String value | 10,000 characters |

## Preview Proxy Security

**Archived.** This section describes the Next.js preview proxies in `archive/frontend-next/`. They are not deployed. The production frontend is the Vite SPA in `frontend/`. It calls the AgentProxy Lambda function URL for agent runs and `POST /ui-actions` for typed actions. In the Vite SPA, `frontend/src/lib/agentClient.ts` drops a `ui` that fails validation and sets `ui_error`.

The local Next.js agent and action proxies are disabled by default. They return `404` unless the server-side environment variable `CEI_ENABLE_LOCAL_PROXY` is exactly `true`. These routes are for local preview only. They do not provide production authentication. The static S3 and CloudFront build cannot host Next.js server routes.

At a high level, the agent proxy accepts a request body of at most 64 KiB, accepts an upstream response of at most 8 MiB, and has a 10-minute upstream timeout. It also limits prompts to 12,000 characters. The action proxy accepts request and response bodies of at most 1 MiB and has a 15-second upstream timeout. Both proxies reject invalid JSON and validate data before they return it. When the upstream `ui` fails validation, the agent proxy omits `ui` and adds `ui_error` rather than failing the request (see "Text Fallback" above).

The agent proxy filters export metadata. It returns at most 20 entries. Each entry can contain only `format`, `filename`, `byte_size`, and optional `download_id`. The allowed formats are `pdf`, `docx`, `md`, `csv`, `txt`, and `latex`. The proxy limits filenames to 255 characters, limits download IDs to 256 characters, and requires a nonnegative safe-integer byte size. It removes all other upstream export fields.

## SSE Diagnostic Safety

The repository contains an SSE serialization helper (`backend/agents/shared/transport/sse.py`). The Research CEI UI 1.2 stream does not use it; it uses `backend/agents/shared/streaming/research.py`. The helper can buffer at most 500 events. Public text deltas are truncated to 20,000 characters.

Diagnostic events do not expose private input or execution data:

- Thinking text is replaced with `{"status":"processing"}`.
- Tool names and tool inputs are replaced with `{"status":"tool_running"}`.
- Code and code results are replaced with `{"status":"code_running"}`.
- Error messages are replaced with `The request could not be completed.`.
- Error codes are limited to `invalid_request`, `rate_limited`, `service_unavailable`, and `internal_error`. Any other code becomes `internal_error`.

The helper emits complete CEI UI surfaces as `ui_surface` events. It does not implement progressive patches.

## Transport Status

- Drafting, Analysis, and Pipeline Manager return completed JSON with optional `ui` (version `1.1`).
- Research can stream CEI UI 1.2 events over `POST /chat/stream` (Session Runner). The SPA uses this stream only when `/config.json` has `researchStreamingEnabled: true`.
- On `/chat/stream`, the Session Runner changes the buffered envelope of Drafting, Analysis, or Pipeline Manager into 1.2 events (F9): `surface_begin`, one `component_add` per component (parent first), then `surface_end` with `final: true`. A buffered `awaiting_input` envelope becomes `surface_begin` and one terminal `hitl_request`.
- Typed actions go to `POST /ui-actions` (UiActions Lambda, STREAM mode). It checks session ownership and resumes the owning agent. Research (clarifying question), Drafting (outline approval), Analysis (scope), and Pipeline Manager (plan) pause and resume (F13). A resume returns ONE JSON envelope (`status`, `text`, optional `ui`, `action_response`), not SSE.
- **Stream HITL (F13).** When Research pauses on the stream path, `main.py` ends the stream with a terminal `hitl_request` that holds the question surface. There is no `surface_end` after it. The SPA renders this card and the user can answer it. The answer goes to `POST /ui-actions`.
- **Router ChoicePicker (F9, B80).** When the free-text classifier confidence is below 0.7, the stream holds `surface_begin` and one terminal `hitl_request`. The surface (`version: "1.1"`, `surface_id` `router-choice-<run_id>`, `revision` 1) has one `ChoicePicker` with `id` and `interaction_id` `router-choice`, `multi: false`, and 4 options: `research`, `drafting`, `analysis`, `pipeline_manager`. Its action is `action_id: route-choice`. The user's pick goes to `POST /ui-actions` with the option value as `value`. The pending choice expires after 600 seconds. Then the action returns `status: rejected`.
- Production authentication (Cognito) and history persistence exist.

Still planned: durable checkpoints for the agents other than Research, and progressive (token-by-token) streaming for the agents other than Research. The free-text input in the SPA is in progress. All F9 and F13 transport behavior still needs live AWS verification.

## CEI UI 1.2 progressive streaming

**Status: Partly built.** The Research runtime emits `version: "1.2"` events (`backend/agents/shared/streaming/research.py`). The frontend accepts `"1.0"`, `"1.1"`, and `"1.2"` (`frontend/src/lib/stream/events.ts`). The other agents do not stream progressively yet; the Session Runner sends their completed surface as 1.2 events (F9). This section does not change anything above. The 1.1 contract in this document — the component allowlist, structural limits, artifact security model, grounding rules, and action contract — stays exactly as written and applies unchanged to 1.2.

CEI UI 1.2 adds a transport-level addition only: progressive SSE patch events, so the client can build the answer while the agent is still working instead of receiving one completed surface at the end. Servers emit `version: "1.2"` on new streams; clients and stored history accept `"1.0"`, `"1.1"`, and `"1.2"`. A fully replayed 1.2 stream produces the same `CEIUISurface` object that 1.1 produces today — only the `version` string differs.

The event catalog includes `surface_begin`, `component_add`, `text_delta`, `component_update`, `grounding_update`, `trace_event`, `progress`, `usage`, `hitl_request`, `job_handoff`, `surface_end`, and `error`. The heartbeat is not an event: the Session Runner sends the SSE comment `: heartbeat` every 17 seconds, with no `seq`. The client makes a local `heartbeat` record from it. These events also populate the collapsible "thinking block" (rendered from `trace_event`) and the live token/cost counter (`usage`) called for elsewhere in this project's design.

`job_handoff` is decided once, from the plan step's own duration estimate, before the run streams any part of the answer — a run never switches to a background job mid-stream after content has already started. This same event catalog and transport will also carry a separate single-source "tool agent" chat route (planned, not built); nothing here is specific to the main chat route.

Full event shapes, ordering rules, reconnect behavior, and worked examples: `.agents/specs/streaming-protocol.md`.

## Examples

The first two examples below are `version: "1.0"` surfaces. They remain valid under 1.1 clients and validators, demonstrating the backward-compatibility rule in "Version Rules". The third example is a `version: "1.1"` surface using the new components.

### Final Surface (1.0)

```json
{
  "result": "The review is complete.",
  "text": "The review is complete.",
  "ui": {
    "content_type": "application/vnd.cei.ui+json",
    "protocol": "cei-ui",
    "version": "1.0",
    "surface_id": "research-final",
    "revision": 1,
    "root_id": "root",
    "components": [
      {"id": "root", "type": "Column", "children": ["answer"]},
      {"id": "answer", "type": "Text", "content": "The review is complete.", "variant": "body"}
    ]
  }
}
```

### Interactive ReviewGate Surface (1.0)

```json
{
  "text": "Review the plan before execution.",
  "ui": {
    "content_type": "application/vnd.cei.ui+json",
    "protocol": "cei-ui",
    "version": "1.0",
    "surface_id": "pipeline-review",
    "revision": 3,
    "root_id": "review",
    "components": [
      {
        "id": "review",
        "type": "ReviewGate",
        "interaction_id": "interaction-456",
        "plan": {
          "summary": "Search evidence, compare results, and prepare the report.",
          "steps": [
            {"step_id": "search", "title": "Search evidence", "status": "pending"},
            {"step_id": "compare", "title": "Compare results", "status": "pending"},
            {"step_id": "report", "title": "Prepare report", "status": "pending"}
          ]
        },
        "actions": [
          {"action_id": "approve-plan", "label": "Approve", "intent": "approve"},
          {"action_id": "revise-plan", "label": "Request changes", "intent": "reject"}
        ],
        "require_approval": true
      }
    ]
  }
}
```

Example action request:

```json
{
  "request_id": "req-018f",
  "session_id": "session-123",
  "surface_id": "pipeline-review",
  "revision": 3,
  "interaction_id": "interaction-456",
  "component_id": "review",
  "action_id": "approve-plan",
  "value": null
}
```

### Rich Research Surface (1.1)

See the golden fixture `frontend/scripts/fixtures/cei-ui-1.1-sample.json` for a complete `version: "1.1"` surface that combines `Tabs`, `StatGrid`, `Table`, `Graph`, `Comparison`, `Timeline`, `Artifact`, `Callout`, and `EvidencePanel` in one research answer. Both the Python and the TypeScript validators check this fixture in tests.
