import { NextResponse } from 'next/server';
import { CEIUISurfaceSchema } from '@/lib/cei-ui/schema';
import {
  BodySizeLimitError,
  FetchTimeoutError,
  fetchWithTimeout,
  isLocalPreviewProxyEnabled,
  readJsonWithLimit,
} from '@/lib/server/preview-proxy';

export const runtime = 'nodejs';

const AGENTS = {
  research: 'AGENTCORE_RESEARCH_URL',
  drafting: 'AGENTCORE_DRAFTING_URL',
} as const;

const DOCUMENT_TYPES = ['grant', 'vc_pitch', 'journal', 'whitepaper', 'latex'] as const;
const EXPORT_FORMATS = ['pdf', 'docx', 'md', 'csv', 'txt', 'latex'] as const;
const MAX_AGENT_REQUEST_BYTES = 65_536;
const MAX_AGENT_RESPONSE_BYTES = 8_388_608;
const AGENT_UPSTREAM_TIMEOUT_MS = 600_000;
const MAX_EXPORTS = 20;
const MAX_EXPORT_FILENAME_LENGTH = 255;
const MAX_DOWNLOAD_ID_LENGTH = 256;

type DocumentType = (typeof DOCUMENT_TYPES)[number];
type ExportFormat = (typeof EXPORT_FORMATS)[number];
type BaseAgentRequest = { prompt: string; session_id: string; mode?: string };
type ResearchRequest = BaseAgentRequest & { agent: 'research' };
type DraftingRequest = BaseAgentRequest & {
  agent: 'drafting';
  document_type: DocumentType;
  export_formats: ExportFormat[];
};
type AgentRequest = ResearchRequest | DraftingRequest;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isDocumentType(value: unknown): value is DocumentType {
  return typeof value === 'string' && DOCUMENT_TYPES.includes(value as DocumentType);
}

function isExportFormat(value: unknown): value is ExportFormat {
  return typeof value === 'string' && EXPORT_FORMATS.includes(value as ExportFormat);
}

function isBaseAgentRequest(value: Record<string, unknown>): value is Record<string, unknown> & BaseAgentRequest {
  return typeof value.prompt === 'string' && value.prompt.trim().length > 0 && value.prompt.length <= 12000 &&
    typeof value.session_id === 'string' && value.session_id.length >= 33 &&
    (value.mode === undefined || typeof value.mode === 'string');
}

function isAgentRequest(value: unknown): value is AgentRequest {
  if (!isRecord(value) || !isBaseAgentRequest(value)) return false;
  if (value.agent === 'research') return true;
  return value.agent === 'drafting' && isDocumentType(value.document_type) &&
    Array.isArray(value.export_formats) && value.export_formats.length > 0 &&
    value.export_formats.every(isExportFormat) && new Set(value.export_formats).size === value.export_formats.length;
}

function normalizeResponse(value: unknown) {
  if (!isRecord(value) || typeof value.result !== 'string' || typeof value.text !== 'string') return null;

  const response: Record<string, unknown> = { result: value.result, text: value.text };
  if (value.ui !== undefined) {
    const parsedSurface = CEIUISurfaceSchema.safeParse(value.ui);
    if (parsedSurface.success) {
      response.ui = parsedSurface.data;
    } else {
      // ADR: an invalid ui surface is dropped, not a 502 | the agent's text answer
      // is still usable and the plan requires a graceful text fallback, not an error.
      console.warn('CEI UI surface failed validation', parsedSurface.error.issues.map((issue) => issue.path.join('.')));
      response.ui_error = 'invalid_surface';
    }
  }
  if (value.exports !== undefined) {
    if (!Array.isArray(value.exports) || value.exports.length > MAX_EXPORTS) return null;
    const exports = [];
    for (const item of value.exports) {
      const normalized = normalizeExport(item);
      if (!normalized) return null;
      exports.push(normalized);
    }
    response.exports = exports;
  }
  return response;
}

function normalizeExport(value: unknown) {
  if (!isRecord(value) || !isExportFormat(value.format) ||
      typeof value.filename !== 'string' || value.filename.length < 1 || value.filename.length > MAX_EXPORT_FILENAME_LENGTH ||
      typeof value.byte_size !== 'number' || !Number.isSafeInteger(value.byte_size) || value.byte_size < 0 ||
      (value.download_id !== undefined &&
        (typeof value.download_id !== 'string' || value.download_id.length < 1 || value.download_id.length > MAX_DOWNLOAD_ID_LENGTH))) {
    return null;
  }
  return {
    format: value.format,
    filename: value.filename,
    byte_size: value.byte_size,
    ...(value.download_id !== undefined ? { download_id: value.download_id } : {}),
  };
}

export async function POST(request: Request) {
  if (!isLocalPreviewProxyEnabled()) {
    return NextResponse.json({ error: 'Not found.' }, { status: 404 });
  }

  let payload: unknown;
  try {
    payload = await readJsonWithLimit(request, MAX_AGENT_REQUEST_BYTES);
  } catch (error) {
    if (error instanceof BodySizeLimitError) {
      return NextResponse.json({ error: 'Request body is too large.' }, { status: 413 });
    }
    return NextResponse.json({ error: 'Request body must be valid JSON.' }, { status: 400 });
  }

  if (!isAgentRequest(payload)) return NextResponse.json({ error: 'Invalid agent request.' }, { status: 400 });

  const upstreamUrl = process.env[AGENTS[payload.agent]];
  if (!upstreamUrl) return NextResponse.json({ error: 'The selected agent is not configured.' }, { status: 503 });

  try {
    const upstreamPayload = {
      prompt: payload.prompt.trim(),
      session_id: payload.session_id,
      ...(payload.mode ? { mode: payload.mode } : {}),
      ...(payload.agent === 'drafting'
        ? { document_type: payload.document_type, export_formats: payload.export_formats }
        : {}),
    };
    const upstream = await fetchWithTimeout(upstreamUrl, {
      method: 'POST',
      headers: { 'content-type': 'application/json', accept: 'application/json' },
      body: JSON.stringify(upstreamPayload),
      cache: 'no-store',
    }, AGENT_UPSTREAM_TIMEOUT_MS);
    const body = await readJsonWithLimit(upstream, MAX_AGENT_RESPONSE_BYTES);
    if (!upstream.ok) return NextResponse.json({ error: 'The agent request failed.' }, { status: upstream.status });

    const normalized = normalizeResponse(body);
    if (!normalized) return NextResponse.json({ error: 'The agent returned an invalid response.' }, { status: 502 });
    return NextResponse.json(normalized);
  } catch (error) {
    if (error instanceof FetchTimeoutError) {
      return NextResponse.json({ error: 'The agent service timed out.' }, { status: 504 });
    }
    if (error instanceof BodySizeLimitError || error instanceof SyntaxError) {
      return NextResponse.json({ error: 'The agent returned an invalid response.' }, { status: 502 });
    }
    return NextResponse.json({ error: 'The agent service is unavailable.' }, { status: 502 });
  }
}