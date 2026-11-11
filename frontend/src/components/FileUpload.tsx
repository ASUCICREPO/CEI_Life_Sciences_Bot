import { useState, useRef, useCallback } from 'react';
import { useSessionsClient } from '../lib/useSessions';

const ALLOWED_TYPES: Record<string, string> = {
  'application/pdf': '.pdf',
  'text/plain': '.txt',
  'text/markdown': '.md',
  'text/csv': '.csv',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': '.xlsx',
};
const ACCEPT = Object.values(ALLOWED_TYPES).join(',');
const MAX_SIZE = 55 * 1024 * 1024; // 55 MB

interface FileUploadProps {
  sessionId: string;
  onUploaded?: (attachmentId: string, filename: string) => void;
  disabled?: boolean;
}

export function FileUpload({ sessionId, onUploaded, disabled }: FileUploadProps) {
  const client = useSessionsClient();
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const upload = useCallback(async (file: File) => {
    if (!client) return;
    setError(null);
    if (file.size > MAX_SIZE) {
      setError('File too large (max 55 MB)');
      return;
    }
    const contentType = file.type || 'application/octet-stream';
    if (!ALLOWED_TYPES[contentType]) {
      setError('Unsupported file type');
      return;
    }
    setUploading(true);
    try {
      const { upload_url, attachment_id } = await client.presignAttachment(sessionId, file.name, contentType);
      const resp = await fetch(upload_url, { method: 'PUT', body: file, headers: { 'Content-Type': contentType } });
      if (!resp.ok) throw new Error(`Upload failed: ${resp.status}`);
      onUploaded?.(attachment_id, file.name);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Upload failed');
    } finally {
      setUploading(false);
      if (inputRef.current) inputRef.current.value = '';
    }
  }, [client, sessionId, onUploaded]);

  const handleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file) upload(file);
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    const file = e.dataTransfer.files?.[0];
    if (file) upload(file);
  };

  return (
    <div
      onDragOver={(e) => e.preventDefault()}
      onDrop={handleDrop}
      className="relative"
    >
      <input
        ref={inputRef}
        type="file"
        accept={ACCEPT}
        onChange={handleChange}
        disabled={disabled || uploading}
        className="hidden"
        id="file-upload"
      />
      <label
        htmlFor="file-upload"
        className={`cursor-pointer inline-flex items-center gap-1 px-2 py-1 text-sm rounded border border-gray-300 dark:border-gray-600 hover:bg-gray-100 dark:hover:bg-gray-700 ${disabled || uploading ? 'opacity-50 cursor-not-allowed' : ''}`}
      >
        {uploading ? (
          <span className="animate-spin">⏳</span>
        ) : (
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15.172 7l-6.586 6.586a2 2 0 102.828 2.828l6.414-6.586a4 4 0 00-5.656-5.656l-6.415 6.585a6 6 0 108.486 8.486L20.5 13" />
          </svg>
        )}
        <span className="sr-only">Attach file</span>
      </label>
      {error && <span className="ml-2 text-sm text-red-500">{error}</span>}
    </div>
  );
}
