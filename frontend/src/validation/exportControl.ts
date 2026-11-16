import { z } from 'zod';
import { CONTROLLING_CLASSIFICATIONS } from '../types/exportControl';

const CLASSIFICATION_VALUES = [
  'public',
  'internal',
  'business_confidential',
  'fci',
  'cui',
  'itar_export_controlled',
  'restricted_customer',
  'unknown',
] as const;

/**
 * Add / edit a customer contract (Customers → Export control). Mirrors
 * `CustomerContractCreate` / `CustomerContractUpdate`: name 1..255, number
 * ≤ 120, handling instructions ≤ 10,000, and a REQUIRED reason ≤ 1,000 that the
 * server records on the required audit row. Dates are Central calendar days
 * (`YYYY-MM-DD`, '' = none) converted to UTC instants by the dialog.
 */
export const customerContractSchema = z
  .object({
    contract_name: z.string().trim().min(1, 'A contract name is required.').max(255, 'At most 255 characters.'),
    contract_number: z.string().trim().max(120, 'At most 120 characters.'),
    itar_controlled: z.boolean(),
    export_controlled: z.boolean(),
    contains_cui: z.boolean(),
    default_data_classification: z.enum(CLASSIFICATION_VALUES),
    effective_date: z.string(),
    expiration_date: z.string(),
    active: z.boolean(),
    handling_instructions: z.string().max(10_000, 'At most 10,000 characters.'),
    reason: z.string().trim().min(1, 'A reason is required.').max(1000, 'At most 1,000 characters.'),
  })
  .superRefine((data, ctx) => {
    if (data.effective_date && data.expiration_date && data.expiration_date < data.effective_date) {
      ctx.addIssue({
        code: 'custom',
        path: ['expiration_date'],
        message: 'The expiration date cannot be before the effective date.',
      });
    }
  });

export type CustomerContractFormInput = z.input<typeof customerContractSchema>;
export type CustomerContractFormData = z.output<typeof customerContractSchema>;

/** Would a contract with these values make its customer export-controlled? (server rule) */
export function contractValuesControl(
  values: Pick<
    CustomerContractFormInput,
    'itar_controlled' | 'export_controlled' | 'contains_cui' | 'default_data_classification'
  >
): boolean {
  return (
    values.itar_controlled ||
    values.export_controlled ||
    values.contains_cui ||
    CONTROLLING_CLASSIFICATIONS.has(values.default_data_classification)
  );
}
