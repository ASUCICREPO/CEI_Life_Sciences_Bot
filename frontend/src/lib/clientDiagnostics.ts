/** Minimal, bounded browser failure signals sent only to the authenticated CEI API. */
export type DiagnosticSource = 'agent' | 'sessions' | 'stream' | 'window' | 'auth';
export type DiagnosticFailure = 'network' | 'timeout' | 'http_error' | 'invalid_response' | 'stream_dropped' | 'unhandled_exception' | 'render_error' | 'auth_error';

type Config = { baseUrl: string; getToken: () => Promise<string | null>; fetchImpl?: typeof fetch };
let config: Config | null = null;
let sent = 0;
let flushing = false;
let retryRequested = false;
const pending: Array<{ source: DiagnosticSource; failure: DiagnosticFailure; status?: number; request_id?: string }> = [];
const MAX_PER_PAGE = 10;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function configureClientDiagnostics(value: Config | null): void {
  config = value;
  if (!value) { pending.length = 0; sent = 0; retryRequested = false; }
  else void flush();
}

async function flush(): Promise<void> {
  if (flushing || !config || pending.length === 0) return;
  const current = config;
  flushing = true;
  try {
    const token = await current.getToken();
    if (!token || config !== current) return;
    while (pending.length && config === current) {
      const item = pending[0];
      const response = await (current.fetchImpl ?? fetch)(`${current.baseUrl.replace(/\/+$/, '')}/client-diagnostics`, {
        method: 'POST', headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' },
        body: JSON.stringify(item), keepalive: true,
      });
      if (!response.ok) return;
      if (pending[0] === item) pending.shift();
    }
  } catch {
    // Retain only the bounded, non-sensitive codes for an online retry.
  } finally {
    flushing = false;
    const retry = retryRequested;
    retryRequested = false;
    if (config && pending.length && (config !== current || retry)) void flush();
  }
}

/** Never pass error objects, URLs, user input, identifiers, or response bodies here. */
export function reportClientFailure(source: DiagnosticSource, failure: DiagnosticFailure, status?: number, requestId?: string): void {
  if (!config || sent >= MAX_PER_PAGE) return;
  sent += 1;
  const payload = {
    source, failure,
    ...(Number.isInteger(status) && status! >= 400 && status! <= 599 ? { status } : {}),
    ...(requestId && UUID.test(requestId) ? { request_id: requestId } : {}),
  };
  pending.push(payload);
  void flush();
}

export function installGlobalDiagnostics(): () => void {
  const onError = () => reportClientFailure('window', 'unhandled_exception');
  const onRejection = () => reportClientFailure('window', 'unhandled_exception');
  const onOnline = () => { if (flushing) retryRequested = true; else void flush(); };
  window.addEventListener('error', onError);
  window.addEventListener('unhandledrejection', onRejection);
  window.addEventListener('online', onOnline);
  return () => {
    window.removeEventListener('error', onError);
    window.removeEventListener('unhandledrejection', onRejection);
    window.removeEventListener('online', onOnline);
  };
}
