'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import type { ArtifactComponent, JsonValue, TableComponent } from '@/lib/cei-ui/schema';
import { ARTIFACT_MAX_HEIGHT, ARTIFACT_MIN_HEIGHT } from '@/lib/cei-ui/schema';
import { TableView } from './table';

const RESIZE_EVENT_TYPE = 'cei-artifact-resize';

// ADR: kill switch defaults to enabled | only the literal string "false" disables
// artifacts, so an unset or misconfigured env var fails open to the current
// (already-validated, sandboxed) behavior rather than silently hiding content.
const ARTIFACTS_ENABLED = process.env.NEXT_PUBLIC_CEI_ENABLE_ARTIFACTS !== 'false';

const HOST_ARTIFACT_STYLES = `
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 12px; font-family: system-ui, sans-serif; color: #17231f; background: #ffffff; overflow-x: hidden; }
  img, svg, canvas { max-width: 100%; }
`;

function escapeForScriptContext(value: string): string {
  return value
    .replaceAll('&', '\\u0026')
    .replaceAll('<', '\\u003c')
    .replaceAll('>', '\\u003e')
    .replaceAll(' ', '\\u2028')
    .replaceAll(' ', '\\u2029');
}

function buildResizeReporterScript(): string {
  return `(function () {
    var target = document.documentElement;
    function report() {
      var height = Math.ceil(target.getBoundingClientRect().height);
      parent.postMessage({ type: ${JSON.stringify(RESIZE_EVENT_TYPE)}, height: height }, '*');
    }
    if (typeof ResizeObserver !== 'undefined') {
      new ResizeObserver(report).observe(target);
    }
    report();
  })();`;
}

// Host-built document. The CSP meta tag, host styles, host data script and host
// resize reporter are all trusted; only the html fragment in between is
// model-authored and was already checked against the denylist by the schema.
function buildArtifactSrcDoc(html: string, data: JsonValue): string {
  const dataJson = escapeForScriptContext(JSON.stringify(data ?? null));
  const csp = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:; connect-src 'none'; media-src 'none'; object-src 'none'; frame-src 'none'; child-src 'none'; worker-src 'none'; form-action 'none'; base-uri 'none'";
  return `<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="${csp}">
<style>${HOST_ARTIFACT_STYLES}</style>
</head>
<body>
<script type="application/json" id="cei-data">${dataJson}</script>
${html}
<script>${buildResizeReporterScript()}</script>
</body>
</html>`;
}

function clampHeight(value: number): number {
  return Math.min(ARTIFACT_MAX_HEIGHT, Math.max(ARTIFACT_MIN_HEIGHT, Math.round(value)));
}

function toDataTable(data: JsonValue): TableComponent | null {
  if (typeof data !== 'object' || data === null || Array.isArray(data)) return null;
  const record = data as Record<string, JsonValue>;
  const columns = record.columns;
  const rows = record.rows;
  if (!Array.isArray(columns) || !columns.every((column) => typeof column === 'string') || columns.length === 0) return null;
  if (!Array.isArray(rows)) return null;
  const width = columns.length;
  const safeRows: (string | number | boolean | null)[][] = [];
  for (const row of rows) {
    if (!Array.isArray(row) || row.length !== width) return null;
    if (!row.every((cell) => cell === null || typeof cell === 'string' || typeof cell === 'number' || typeof cell === 'boolean')) return null;
    safeRows.push(row as (string | number | boolean | null)[]);
  }
  return {
    id: 'artifact-data-preview',
    type: 'Table',
    title: null,
    caption: null,
    columns: columns.map((label, index) => ({ key: `c${index}`, label, align: 'left' as const, kind: 'text' as const })),
    rows: safeRows,
    flagged_cells: [],
    sortable: false,
  };
}

export function ArtifactView({ component }: { component: ArtifactComponent }) {
  const dataTable = useMemo(() => toDataTable(component.data), [component.data]);
  return (
    <figure className="cei-artifact">
      <figcaption className="cei-artifact-title">{component.title}</figcaption>
      <p className="cei-artifact-description">{component.description}</p>
      {ARTIFACTS_ENABLED && <ArtifactFrame key={component.id} html={component.html} data={component.data} height={component.height} title={component.title} />}
      {dataTable && <TableView component={dataTable} />}
    </figure>
  );
}

function ArtifactFrame({ html, data, height, title }: { html: string; data: JsonValue; height: number; title: string }) {
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const [frameHeight, setFrameHeight] = useState(() => clampHeight(height));
  const [blocked, setBlocked] = useState(false);
  const loadCountRef = useRef(0);
  const rafRef = useRef<number | null>(null);
  const pendingHeightRef = useRef<number | null>(null);
  const srcDoc = useMemo(() => buildArtifactSrcDoc(html, data), [html, data]);

  useEffect(() => {
    function handleMessage(event: MessageEvent) {
      const frame = iframeRef.current;
      // ADR: accept resize messages only from this frame, with origin "null"
      // (sandboxed srcdoc has an opaque origin) and an exact {type, height} shape.
      if (!frame || event.source !== frame.contentWindow || event.origin !== 'null') return;
      const payload: unknown = event.data;
      if (typeof payload !== 'object' || payload === null || Array.isArray(payload)) return;
      const keys = Object.keys(payload);
      if (keys.length !== 2 || !keys.includes('type') || !keys.includes('height')) return;
      const typed = payload as { type: unknown; height: unknown };
      if (typed.type !== RESIZE_EVENT_TYPE || typeof typed.height !== 'number' || !Number.isFinite(typed.height)) return;
      pendingHeightRef.current = clampHeight(typed.height);
      if (rafRef.current !== null) return;
      rafRef.current = requestAnimationFrame(() => {
        rafRef.current = null;
        if (pendingHeightRef.current !== null) setFrameHeight(pendingHeightRef.current);
      });
    }
    window.addEventListener('message', handleMessage);
    return () => {
      window.removeEventListener('message', handleMessage);
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
    };
  }, []);

  function handleLoad() {
    loadCountRef.current += 1;
    // ADR: navigation tripwire | a sandboxed srcdoc iframe without allow-same-origin
    // cannot navigate the top frame, but it can navigate itself; a second onLoad
    // means the model html tried to leave the srcdoc, so we unmount it.
    if (loadCountRef.current > 1) setBlocked(true);
  }

  if (blocked) return <p className="cei-artifact-blocked" role="alert">Interactive view was blocked.</p>;

  return (
    <iframe
      ref={iframeRef}
      className="cei-artifact-frame"
      sandbox="allow-scripts"
      srcDoc={srcDoc}
      title={title}
      referrerPolicy="no-referrer"
      loading="lazy"
      allow="camera 'none'; microphone 'none'; geolocation 'none'; clipboard-read 'none'; clipboard-write 'none'; fullscreen 'none'; payment 'none'; usb 'none'"
      style={{ height: frameHeight }}
      onLoad={handleLoad}
    />
  );
}
