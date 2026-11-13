import { useEffect, useMemo, useState } from 'react';
import { Download, FileText } from 'lucide-react';
import { useAuth } from '../lib/auth/AuthProvider';
import { createExportsClient, type ReportExport } from '../lib/exportsClient';

export function ReportExports({ sessionId }: { sessionId: string }) {
  const { getAccessToken, config } = useAuth();
  const client = useMemo(() => createExportsClient(getAccessToken, config?.apiBaseUrl), [getAccessToken, config?.apiBaseUrl]);
  const [items, setItems] = useState<ReportExport[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setItems([]);
    setCursor(null);
    setError(null);
    client.list(sessionId).then((page) => {
      if (!active) return;
      setItems(page.items);
      setCursor(page.next_cursor);
    }).catch(() => {
      if (active) setError('Could not load reports. Please try again.');
    }).finally(() => {
      if (active) setLoading(false);
    });
    return () => { active = false; };
  }, [client, sessionId, reload]);

  async function loadMore() {
    if (!cursor || loading) return;
    setLoading(true);
    setError(null);
    try {
      const page = await client.list(sessionId, cursor);
      setItems((current) => [...current, ...page.items]);
      setCursor(page.next_cursor);
    } catch {
      setError('Could not load more reports. Please try again.');
    } finally { setLoading(false); }
  }

  async function download(item: ReportExport) {
    setBusyId(item.export_id);
    setError(null);
    try {
      const url = await client.download(item.export_id);
      const link = document.createElement('a');
      link.href = url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.click();
    } catch {
      setError('Could not download this report. Please try again.');
    } finally { setBusyId(null); }
  }

  return (
    <section aria-label="Generated reports" className="border-b border-border-subtle bg-surface-elevated px-6 py-4">
      <div className="max-w-3xl mx-auto">
        <h2 className="text-sm font-semibold text-text-primary">Generated reports</h2>
        {loading && items.length === 0 && <p role="status" className="mt-2 text-xs text-text-secondary">Loading reports…</p>}
        {!loading && !error && items.length === 0 && <p className="mt-2 text-xs text-text-secondary">No reports in this chat yet.</p>}
        {error && <p role="alert" className="mt-2 text-xs text-evidence-weak">{error}{items.length === 0 && <button type="button" className="ml-2 underline" onClick={() => setReload((n) => n + 1)}>Retry</button>}</p>}
        {items.length > 0 && <ul className="mt-2 divide-y divide-border-subtle">
          {items.map((item) => <li key={item.export_id} className="flex items-center gap-3 py-2">
            <FileText size={16} className="shrink-0 text-text-tertiary" aria-hidden="true" />
            <span className="min-w-0 flex-1 truncate text-sm text-text-primary">{item.filename}</span>
            <button type="button" disabled={busyId !== null} onClick={() => void download(item)}
              className="inline-flex items-center gap-1 text-xs font-medium text-cei-blue hover:underline disabled:opacity-50"
              aria-label={`Download ${item.filename}`}>
              <Download size={14} aria-hidden="true" /> {busyId === item.export_id ? 'Preparing…' : 'Download'}
            </button>
          </li>)}
        </ul>}
        {cursor && <button type="button" disabled={loading} onClick={() => void loadMore()} className="mt-2 text-xs text-cei-blue hover:underline disabled:opacity-50">{loading ? 'Loading…' : 'Load more'}</button>}
      </div>
    </section>
  );
}
