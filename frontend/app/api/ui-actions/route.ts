import { NextResponse } from 'next/server';
import { ActionRequestSchema, ActionResponseSchema } from '@/lib/cei-ui/schema';
import { actionResponseMatchesRequest } from '@/lib/cei-ui/action-correlation';
import {
  BodySizeLimitError,
  FetchTimeoutError,
  fetchWithTimeout,
  isLocalPreviewProxyEnabled,
  readJsonWithLimit,
} from '@/lib/server/preview-proxy';

export const runtime = 'nodejs';

const MAX_ACTION_REQUEST_BYTES = 1_048_576;
const MAX_ACTION_RESPONSE_BYTES = 1_048_576;
const ACTION_UPSTREAM_TIMEOUT_MS = 15_000;

export async function POST(request: Request) {
  if (!isLocalPreviewProxyEnabled()) {
    return NextResponse.json({ error: 'Not found.' }, { status: 404 });
  }

  let body: unknown;
  try {
    body = await readJsonWithLimit(request, MAX_ACTION_REQUEST_BYTES);
  } catch (error) {
    if (error instanceof BodySizeLimitError) {
      return NextResponse.json({ error: 'Request body is too large.' }, { status: 413 });
    }
    return NextResponse.json({ error: 'Request body must be valid JSON.' }, { status: 400 });
  }

  const parsedRequest = ActionRequestSchema.safeParse(body);
  if (!parsedRequest.success) return NextResponse.json({ error: 'Invalid CEI UI action request.' }, { status: 400 });

  const upstreamUrl = process.env.AGENTCORE_ACTION_URL;
  if (!upstreamUrl) return NextResponse.json({ error: 'The action service is not configured.' }, { status: 503 });

  try {
    const upstream = await fetchWithTimeout(upstreamUrl, {
      method: 'POST',
      headers: { 'content-type': 'application/json', accept: 'application/json' },
      body: JSON.stringify(parsedRequest.data),
      cache: 'no-store',
    }, ACTION_UPSTREAM_TIMEOUT_MS);
    const upstreamBody = await readJsonWithLimit(upstream, MAX_ACTION_RESPONSE_BYTES);
    const parsedResponse = ActionResponseSchema.safeParse(upstreamBody);
    if (!parsedResponse.success) {
      return NextResponse.json({ error: 'The action service returned an invalid response.' }, { status: 502 });
    }
    if (!actionResponseMatchesRequest(parsedRequest.data, parsedResponse.data)) {
      return NextResponse.json({ error: 'The action response did not match the request.' }, { status: 502 });
    }
    return NextResponse.json(parsedResponse.data, { status: upstream.status });
  } catch (error) {
    if (error instanceof FetchTimeoutError) {
      return NextResponse.json({ error: 'The action service timed out.' }, { status: 504 });
    }
    if (error instanceof BodySizeLimitError || error instanceof SyntaxError) {
      return NextResponse.json({ error: 'The action service returned an invalid response.' }, { status: 502 });
    }
    return NextResponse.json({ error: 'The action service is unavailable.' }, { status: 502 });
  }
}