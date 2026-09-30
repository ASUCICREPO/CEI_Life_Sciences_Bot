'use client';

import { FormEvent, useEffect, useState } from 'react';
import { CEIUISurface } from '@/components/cei-ui-surface';
import { SafeMarkdown } from '@/components/cei-ui/markdown';
import { safeParseCEIUISurface, type ActionResponse, type CEIUISurface as CEIUISurfaceValue } from '@/lib/cei-ui/schema';

type AgentType = 'research' | 'drafting';
type DocumentType = 'grant' | 'vc_pitch' | 'journal' | 'whitepaper' | 'latex';
type ExportFormat = 'pdf' | 'docx' | 'md' | 'csv' | 'txt' | 'latex';
type AgentRequest =
  | { agent: 'research'; prompt: string; session_id: string; mode: 'science' }
  | { agent: 'drafting'; prompt: string; session_id: string; mode: 'science'; document_type: DocumentType; export_formats: ExportFormat[] };
type AgentResponse = {
  result: string;
  text: string;
  ui?: CEIUISurfaceValue;
  ui_error?: string;
  exports?: unknown[];
};

const SESSION_KEY = 'cei-ui-session-id';
const DOCUMENT_TYPES: { value: DocumentType; label: string }[] = [
  { value: 'grant', label: 'Grant proposal' },
  { value: 'vc_pitch', label: 'VC pitch' },
  { value: 'journal', label: 'Journal manuscript' },
  { value: 'whitepaper', label: 'Whitepaper' },
  { value: 'latex', label: 'LaTeX document' },
];
const EXPORT_FORMATS: { value: ExportFormat; label: string }[] = [
  { value: 'pdf', label: 'PDF' },
  { value: 'docx', label: 'Word document' },
  { value: 'md', label: 'Markdown' },
  { value: 'csv', label: 'CSV' },
  { value: 'txt', label: 'Plain text' },
  { value: 'latex', label: 'LaTeX' },
];

function getSessionId() {
  const existing = window.sessionStorage.getItem(SESSION_KEY);
  if (existing) return existing;
  const sessionId = `session_${Date.now()}_${crypto.randomUUID().replaceAll('-', '')}`;
  window.sessionStorage.setItem(SESSION_KEY, sessionId);
  return sessionId;
}

export default function Home() {
  const [agent, setAgent] = useState<AgentType>('research');
  const [prompt, setPrompt] = useState('What are the latest CRISPR base editing approaches for sickle cell disease?');
  const [documentType, setDocumentType] = useState<DocumentType>('grant');
  const [exportFormats, setExportFormats] = useState<ExportFormat[]>(['md', 'txt']);
  const [sessionId, setSessionId] = useState('');
  const [response, setResponse] = useState<AgentResponse>();
  const [error, setError] = useState('');
  const [actionNotice, setActionNotice] = useState('');
  const [isLoading, setIsLoading] = useState(false);

  useEffect(() => setSessionId(getSessionId()), []);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!prompt.trim() || !sessionId || (agent === 'drafting' && exportFormats.length === 0)) return;
    setIsLoading(true);
    setError('');
    setActionNotice('');
    setResponse(undefined);
    try {
      const requestPayload: AgentRequest = agent === 'drafting'
        ? { agent, prompt, session_id: sessionId, mode: 'science', document_type: documentType, export_formats: exportFormats }
        : { agent, prompt, session_id: sessionId, mode: 'science' };
      const apiResponse = await fetch('/api/agent', {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify(requestPayload),
      });
      const body: unknown = await apiResponse.json();
      const parsedResponse = parseAgentResponse(body);
      if (!apiResponse.ok || !parsedResponse) {
        setError(isErrorResponse(body) ? body.error : 'The request could not be completed.');
        return;
      }
      setResponse(parsedResponse);
    } catch {
      setError('Unable to contact the local agent proxy.');
    } finally {
      setIsLoading(false);
    }
  }

  function toggleExportFormat(format: ExportFormat) {
    setExportFormats((currentFormats) => currentFormats.includes(format)
      ? currentFormats.filter((currentFormat) => currentFormat !== format)
      : [...currentFormats, format]);
  }

  function handleAction(response: ActionResponse) {
    setActionNotice(response.message ?? `Action ${response.status}.`);
  }

  return (
    <main className="shell">
      <header className="masthead">
        <div><p className="eyebrow">CEI Life Sciences</p><h1>AgentCore CEI UI preview</h1></div>
        <span className="session" title={sessionId || 'Preparing session'}>{sessionId ? `Session ${sessionId.slice(-10)}` : 'Preparing session'}</span>
      </header>
      <section className="workspace" aria-label="Agent preview workspace">
        <form className="prompt-panel" onSubmit={submit}>
          <fieldset disabled={isLoading}><legend>Choose an agent</legend><div className="agent-options">
            <label className={agent === 'research' ? 'agent-option active' : 'agent-option'}><input type="radio" name="agent" value="research" checked={agent === 'research'} onChange={() => setAgent('research')} />Research</label>
            <label className={agent === 'drafting' ? 'agent-option active' : 'agent-option'}><input type="radio" name="agent" value="drafting" checked={agent === 'drafting'} onChange={() => setAgent('drafting')} />Drafting</label>
          </div></fieldset>
          {agent === 'drafting' && <fieldset className="drafting-options" disabled={isLoading}>
            <legend>Draft settings</legend>
            <label htmlFor="document-type">Document type</label>
            <select id="document-type" value={documentType} onChange={(event) => setDocumentType(event.target.value as DocumentType)}>
              {DOCUMENT_TYPES.map((document) => <option key={document.value} value={document.value}>{document.label}</option>)}
            </select>
            <span className="input-label">Export formats</span>
            <div className="export-options">
              {EXPORT_FORMATS.map((format) => <label key={format.value} className="export-option"><input type="checkbox" checked={exportFormats.includes(format.value)} onChange={() => toggleExportFormat(format.value)} />{format.label}</label>)}
            </div>
          </fieldset>}
          <label htmlFor="prompt">Prompt</label>
          <textarea id="prompt" value={prompt} onChange={(event) => setPrompt(event.target.value)} maxLength={12000} required />
          <button type="submit" disabled={isLoading || !sessionId || (agent === 'drafting' && exportFormats.length === 0)}>{isLoading ? 'Running agent...' : 'Run agent'}</button>
          {error && <p className="form-error" role="alert">{error}</p>}
        </form>
        <section className="response-panel" aria-live="polite">
          {!response && !isLoading && <p className="empty-state">Send a prompt to inspect the agent response and any generated CEI UI surface.</p>}
          {isLoading && <p className="empty-state">Waiting for the selected AgentCore runtime.</p>}
          {response && <>{response.ui
            ? <CEIUISurface surface={response.ui} sessionId={sessionId} fallbackText={response.text} onAction={handleAction} />
            : <>
              {response.ui_error && <p className="preview-error" role="status">Rich view unavailable; showing text.</p>}
              <SafeMarkdown content={response.text} className="cei-markdown response-text" />
            </>}
          {actionNotice && <p className="action-notice" role="status">{actionNotice}</p>}
          {response.exports && response.exports.length > 0 && <p className="export-count">{response.exports.length} export{response.exports.length === 1 ? '' : 's'} returned</p>}</>}
        </section>
      </section>
    </main>
  );
}

function parseAgentResponse(value: unknown): AgentResponse | null {
  if (!isRecord(value) || typeof value.result !== 'string' || typeof value.text !== 'string') return null;
  let ui: CEIUISurfaceValue | undefined;
  // ADR: an invalid ui surface drops the surface but keeps the text response
  // rather than failing the whole response | plan requires a text fallback.
  let uiError = typeof value.ui_error === 'string' ? value.ui_error : undefined;
  if (value.ui !== undefined) {
    const parsedSurface = safeParseCEIUISurface(value.ui);
    if (parsedSurface.success) ui = parsedSurface.data;
    else uiError = 'invalid_surface';
  }
  return {
    result: value.result,
    text: value.text,
    ...(ui ? { ui } : {}),
    ...(uiError ? { ui_error: uiError } : {}),
    ...(Array.isArray(value.exports) ? { exports: value.exports } : {}),
  };
}

function isErrorResponse(value: unknown): value is { error: string } {
  return isRecord(value) && typeof value.error === 'string';
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}
