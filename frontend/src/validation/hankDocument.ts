import { z } from 'zod';

export const HANK_PDF_MAX_BYTES = 25_000_000;

export const hankDocumentSchema = z.object({
  title: z.string().trim().min(1, 'Enter a document title.').max(255, 'Use 255 characters or fewer.'),
  document_type: z.string().min(1, 'Choose a document type.'),
  revision: z.string().trim().min(1, 'Enter a revision.').max(20, 'Use 20 characters or fewer.'),
  description: z.string().trim().max(4000, 'Use 4,000 characters or fewer.'),
});

export type HankDocumentFields = z.infer<typeof hankDocumentSchema>;

export function hankPdfError(file: File): string | null {
  if (
    !/\.pdf$/i.test(file.name) ||
    (file.type && !['application/pdf', 'application/octet-stream'].includes(file.type))
  ) {
    return 'Choose a PDF file.';
  }
  if (!file.size) return 'This PDF is empty. Choose a file with content.';
  if (file.size > HANK_PDF_MAX_BYTES) return 'Choose a PDF smaller than 25 MB.';
  return null;
}
