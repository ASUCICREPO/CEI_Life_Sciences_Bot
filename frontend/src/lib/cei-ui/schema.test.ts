import { describe, expect, it } from 'vitest';
import { ExportFileComponentSchema, safeParseCEIUISurface } from './schema';

const VALID_EXPORT_ID = '12345678-1234-1234-1234-123456789abc_0123456789abcdef0123456789abcdef';

describe('ExportFileComponentSchema', () => {
  const base = {
    id: 'file-1',
    type: 'ExportFile' as const,
    export_id: VALID_EXPORT_ID,
    filename: 'report.pdf',
    format: 'pdf' as const,
    byte_size: 1024,
  };

  it('accepts a valid ExportFile component', () => {
    expect(ExportFileComponentSchema.safeParse(base).success).toBe(true);
  });

  it.each(['pdf', 'docx', 'md', 'txt', 'latex', 'csv'])('accepts format %s', (format) => {
    expect(ExportFileComponentSchema.safeParse({ ...base, format }).success).toBe(true);
  });

  it('rejects an unknown format', () => {
    expect(ExportFileComponentSchema.safeParse({ ...base, format: 'docx2' }).success).toBe(false);
  });

  it('rejects an export_id that does not match the UUID_hex32 shape', () => {
    expect(ExportFileComponentSchema.safeParse({ ...base, export_id: 'not-an-export-id' }).success).toBe(false);
    expect(ExportFileComponentSchema.safeParse({ ...base, export_id: VALID_EXPORT_ID.toUpperCase() }).success).toBe(false);
  });

  it('rejects an empty or over-long filename', () => {
    expect(ExportFileComponentSchema.safeParse({ ...base, filename: '' }).success).toBe(false);
    expect(ExportFileComponentSchema.safeParse({ ...base, filename: 'a'.repeat(256) }).success).toBe(false);
  });

  it('rejects a negative or non-integer byte_size', () => {
    expect(ExportFileComponentSchema.safeParse({ ...base, byte_size: -1 }).success).toBe(false);
    expect(ExportFileComponentSchema.safeParse({ ...base, byte_size: 1.5 }).success).toBe(false);
  });

  it('rejects unknown extra fields (strict object)', () => {
    expect(ExportFileComponentSchema.safeParse({ ...base, document_type: 'grant' }).success).toBe(false);
  });

  it('parses as part of a full surface', () => {
    const result = safeParseCEIUISurface({
      content_type: 'application/vnd.cei.ui+json',
      protocol: 'cei-ui',
      version: '1.1',
      surface_id: 's',
      revision: 1,
      root_id: 'file-1',
      components: [base],
    });
    expect(result.success).toBe(true);
  });
});
