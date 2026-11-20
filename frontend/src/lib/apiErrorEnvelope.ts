// Shared api.md error envelope parsing: { error: { code, message, retriable, request_id } }.
// New typed REST clients (profileClient.ts) use this instead of repeating the same four fields;
// existing clients (accountClient.ts, modelsClient.ts, sessionsClient.ts) keep their own inline copy.

export function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
}

export interface ParsedApiError {
  message: string;
  code?: string;
  retriable: boolean;
  requestId?: string;
}

export function parseApiErrorEnvelope(parsed: unknown, response: Response, fallbackMessage: string): ParsedApiError {
  const envelope = asRecord(asRecord(parsed)?.error);
  return {
    message: typeof envelope?.message === 'string' ? envelope.message : fallbackMessage,
    code: typeof envelope?.code === 'string' ? envelope.code : undefined,
    retriable: envelope?.retriable === true || response.status >= 500,
    requestId: typeof envelope?.request_id === 'string' ? envelope.request_id : undefined,
  };
}
