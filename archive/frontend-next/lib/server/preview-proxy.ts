import 'server-only';

export class BodySizeLimitError extends Error {}
export class FetchTimeoutError extends Error {}

export function isLocalPreviewProxyEnabled(): boolean {
  return process.env.CEI_ENABLE_LOCAL_PROXY === 'true';
}

export async function readJsonWithLimit(message: Request | Response, maxBytes: number): Promise<unknown> {
  const contentLength = message.headers.get('content-length');
  if (contentLength !== null && /^\d+$/.test(contentLength) && Number(contentLength) > maxBytes) {
    throw new BodySizeLimitError(`Body exceeds ${maxBytes} bytes.`);
  }
  if (!message.body) throw new SyntaxError('JSON body is required.');

  const reader = message.body.getReader();
  const chunks: Uint8Array[] = [];
  let totalBytes = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    totalBytes += value.byteLength;
    if (totalBytes > maxBytes) {
      await reader.cancel().catch(() => undefined);
      throw new BodySizeLimitError(`Body exceeds ${maxBytes} bytes.`);
    }
    chunks.push(value);
  }

  const bytes = new Uint8Array(totalBytes);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  const text = new TextDecoder('utf-8', { fatal: true }).decode(bytes);
  return JSON.parse(text) as unknown;
}

export async function fetchWithTimeout(
  input: string,
  init: RequestInit,
  timeoutMs: number,
): Promise<Response> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(input, { ...init, signal: controller.signal });
  } catch (error) {
    if (controller.signal.aborted) throw new FetchTimeoutError(`Fetch exceeded ${timeoutMs}ms.`);
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}