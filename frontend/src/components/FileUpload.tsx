/**
 * File upload dropzone component.
 * Handles drag-and-drop and click-to-upload, gets presigned URL, uploads to S3.
 */
import { useCallback, useState, useRef } from 'react';
import { Upload, X, File, CheckCircle, AlertCircle, Loader2 } from 'lucide-react';
import { useSessionsClient } from '../lib/useSessions';

const ALLOWED_TYPES = new Set([
  'application/pdf',
  'text/plain',
  'text/markdown',
  'text/csv',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
]);

const MAX_SIZE_MB = 55;
const MAX_SIZE_BYTES = MAX_SIZE_MB * 1024 * 1024;

type UploadStatus = 'idle' | 'uploading' | 'success' | 'error';

interface FileState {
  file: File;
  status: UploadStatus;
  progress: number;
  error?: string;
  attachmentId?: string;
}

interface FileUploadProps {
  sessionId: string;
  onUploadComplete?: (attachmentId: string, filename: string) => void;
  onUploadError?: (error: string) => void;
  disabled?: boolean;
  compact?: boolean;
}

export function FileUpload({ sessionId, onUploadComplete, onUploadError, disabled, compact }: FileUploadProps) {
  const sessionsClient = useSessionsClient();
  const [files, setFiles] = useState<FileState[]>([]);
  const [isDragging, setIsDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const validateFile = (file: File): string | null => {
    if (!ALLOWED_TYPES.has(file.type)) {
      return `Unsupported file type: ${file.type || 'unknown'}`;
    }
    if (file.size > MAX_SIZE_BYTES) {
      return `File too large (max ${MAX_SIZE_MB}MB)`;
    }
    return null;
  };

  const uploadFile = useCallback(async (file: File) => {
    const error = validateFile(file);
    if (error) {
      setFiles(prev => [...prev, { file, status: 'error', progress: 0, error }]);
      onUploadError?.(error);
      return;
    }

    const fileState: FileState = { file, status: 'uploading', progress: 0 };
    setFiles(prev => [...prev, fileState]);

    try {
      // Get presigned URL via sessions client
      const { upload_url, attachment_id } = await sessionsClient.presignAttachment({
        filename: file.name,
        content_type: file.type,
        session_id: sessionId,
      });

      // Upload directly to S3
      const uploadRes = await fetch(upload_url, {
        method: 'PUT',
        headers: { 'Content-Type': file.type },
        body: file,
      });

      if (!uploadRes.ok) {
        throw new Error('Upload to S3 failed');
      }

      setFiles(prev =>
        prev.map(f =>
          f.file === file ? { ...f, status: 'success', progress: 100, attachmentId: attachment_id } : f
        )
      );
      onUploadComplete?.(attachment_id, file.name);
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Upload failed';
      setFiles(prev =>
        prev.map(f => (f.file === file ? { ...f, status: 'error', error: message } : f))
      );
      onUploadError?.(message);
    }
  }, [sessionId, sessionsClient, onUploadComplete, onUploadError]);

  const handleFiles = useCallback((fileList: FileList | null) => {
    if (!fileList) return;
    Array.from(fileList).forEach(uploadFile);
  }, [uploadFile]);

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(false);
    if (disabled) return;
    handleFiles(e.dataTransfer.files);
  }, [handleFiles, disabled]);

  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    if (!disabled) setIsDragging(true);
  }, [disabled]);

  const handleDragLeave = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(false);
  }, []);

  const removeFile = (file: File) => {
    setFiles(prev => prev.filter(f => f.file !== file));
  };

  const statusIcon = (status: UploadStatus) => {
    switch (status) {
      case 'uploading':
        return <Loader2 size={14} className="animate-spin text-cei-blue" />;
      case 'success':
        return <CheckCircle size={14} className="text-success" />;
      case 'error':
        return <AlertCircle size={14} className="text-red-500" />;
      default:
        return <File size={14} className="text-text-secondary" />;
    }
  };

  if (compact) {
    return (
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={() => inputRef.current?.click()}
          disabled={disabled}
          className="p-1.5 rounded-md text-text-secondary hover:text-text-primary hover:bg-surface-panel transition-colors disabled:opacity-50"
          aria-label="Attach file"
        >
          <Upload size={16} />
        </button>
        <input
          ref={inputRef}
          type="file"
          multiple
          accept=".pdf,.txt,.md,.csv,.docx,.xlsx"
          onChange={e => handleFiles(e.target.files)}
          className="hidden"
          disabled={disabled}
        />
        {files.length > 0 && (
          <span className="text-xs text-text-secondary">
            {files.filter(f => f.status === 'success').length} attached
          </span>
        )}
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div
        onDrop={handleDrop}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        onClick={() => !disabled && inputRef.current?.click()}
        className={`
          border-2 border-dashed rounded-xl p-6 text-center cursor-pointer transition-all
          ${isDragging ? 'border-cei-blue bg-cei-blue/5' : 'border-border-subtle hover:border-cei-blue/50'}
          ${disabled ? 'opacity-50 cursor-not-allowed' : ''}
        `}
      >
        <Upload size={24} className="mx-auto mb-2 text-text-secondary" />
        <p className="text-sm text-text-primary font-medium">
          Drop files here or click to upload
        </p>
        <p className="text-xs text-text-tertiary mt-1">
          PDF, TXT, Markdown, CSV, DOCX, XLSX (max {MAX_SIZE_MB}MB)
        </p>
        <input
          ref={inputRef}
          type="file"
          multiple
          accept=".pdf,.txt,.md,.csv,.docx,.xlsx"
          onChange={e => handleFiles(e.target.files)}
          className="hidden"
          disabled={disabled}
        />
      </div>

      {files.length > 0 && (
        <div className="space-y-2">
          {files.map((f, i) => (
            <div
              key={`${f.file.name}-${i}`}
              className="flex items-center gap-2 p-2 rounded-lg bg-surface-panel text-sm"
            >
              {statusIcon(f.status)}
              <span className="flex-1 truncate text-text-primary">{f.file.name}</span>
              {f.error && <span className="text-xs text-red-500 truncate max-w-32">{f.error}</span>}
              <button
                type="button"
                onClick={() => removeFile(f.file)}
                className="p-1 rounded hover:bg-surface-elevated text-text-secondary hover:text-text-primary"
                aria-label="Remove file"
              >
                <X size={14} />
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
