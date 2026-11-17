import { z } from 'zod';

/** The server's name bound (`ReportTemplateCreate.name`, 1-255). */
export const REPORT_TEMPLATE_NAME_MAX = 255;
/** The server's description bound. */
export const REPORT_TEMPLATE_DESCRIPTION_MAX = 2000;

/**
 * Save-as-new / edit form for a saved report template. Mirrors the server's create
 * and PATCH bodies: a name of 1-255 characters (not blank), an optional description of
 * at most 2,000 (blank sends null), the Shared flag, and — on edit only, when the template is open
 * in the builder with changes — whether to replace its stored definition too. The
 * server stays authoritative; anything it still refuses is toasted verbatim.
 */
export const reportTemplateFormSchema = z
  .object({
    name: z
      .string()
      .trim()
      .min(1, 'A name is required.')
      .max(REPORT_TEMPLATE_NAME_MAX, `At most ${REPORT_TEMPLATE_NAME_MAX} characters.`),
    description: z
      .string()
      .refine(
        value => value.trim().length <= REPORT_TEMPLATE_DESCRIPTION_MAX,
        `At most ${REPORT_TEMPLATE_DESCRIPTION_MAX.toLocaleString()} characters.`
      ),
    is_shared: z.boolean(),
    include_definition: z.boolean(),
  })
  .transform(data => ({
    name: data.name,
    description: data.description.trim() === '' ? null : data.description.trim(),
    is_shared: data.is_shared,
    include_definition: data.include_definition,
  }));

export type ReportTemplateFormInput = z.input<typeof reportTemplateFormSchema>;
export type ReportTemplateFormData = z.output<typeof reportTemplateFormSchema>;
