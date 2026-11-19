import { z } from 'zod';

/**
 * Purchase-order quality flow-down settings (Purchasing → Quality flow-down). Limits mirror
 * backend/app/schemas/po_flowdown.py: heading 1..255, document_reference ≤ 100, clause code
 * 1..20, clause title 1..255, any body ≤ 20,000, sort_order 0..1,000,000. Text is stored
 * verbatim — only surrounding whitespace is trimmed, and a blank optional field is sent as null.
 */
const MAX_FLOWDOWN_TEXT = 20_000;
const textBody = z.string().max(MAX_FLOWDOWN_TEXT, 'At most 20,000 characters.');
export const flowdownProfileSchema = z.object({
  enabled: z.boolean(),
  heading: z.string().trim().min(1, 'A heading is required.').max(255, 'At most 255 characters.'),
  document_reference: z.string().trim().max(100, 'At most 100 characters.'),
  body: textBody,
});

export type FlowdownProfileFormInput = z.input<typeof flowdownProfileSchema>;
export type FlowdownProfileFormData = z.output<typeof flowdownProfileSchema>;

const sortOrderField = z
  .string()
  .trim()
  .regex(/^\d{1,7}$/, 'Enter a whole number from 0 to 1,000,000.')
  .transform(value => Number(value))
  .refine(value => value <= 1_000_000, 'Enter a whole number from 0 to 1,000,000.');

/** Add / edit one catalog clause. The code is validated on create only (immutable after). */
export const qualityClauseSchema = z.object({
  code: z.string().trim().min(1, 'A code is required.').max(20, 'At most 20 characters.'),
  title: z.string().trim().min(1, 'A title is required.').max(255, 'At most 255 characters.'),
  body: textBody,
  default_selected: z.boolean(),
  is_active: z.boolean(),
  sort_order: sortOrderField,
});

export type QualityClauseFormInput = z.input<typeof qualityClauseSchema>;
export type QualityClauseFormData = z.output<typeof qualityClauseSchema>;
