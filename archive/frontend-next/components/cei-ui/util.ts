export function isHttpsUrl(value: unknown): value is string {
  if (typeof value !== 'string') return false;
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && url.hostname.length > 0;
  } catch {
    return false;
  }
}

export function assertNever(value: never): never {
  throw new Error(`Unsupported CEI UI value: ${String(value)}`);
}
