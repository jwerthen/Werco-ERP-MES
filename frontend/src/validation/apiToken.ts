import { z } from 'zod';
import { MAX_API_TOKEN_EXPIRES_DAYS } from '../types/apiToken';

/** The server's 422 text for `ai_external` on a full token — shown before the round trip. */
export const AI_EXTERNAL_REQUIRES_READ_ONLY = 'An AI-external API token must be read-only.';

const EXPIRES_DAYS_MESSAGE = `Enter whole days from 1 to ${MAX_API_TOKEN_EXPIRES_DAYS}, or leave blank for never.`;

/**
 * Issue-token form (Admin → API Tokens). Mirrors `ApiTokenCreate`:
 * label 1..100 and not blank, `expires_days` 1..3650 or omitted (never expires),
 * and `ai_external` only on a read-only token. The server stays authoritative —
 * anything it still refuses comes back as a verbatim `detail` toast.
 */
export const apiTokenIssueSchema = z
  .object({
    // ComboBox value (a user id as a string; '' = nothing chosen).
    user_id: z.string().min(1, 'Choose the user this token acts as.'),
    label: z.string().trim().min(1, 'A label is required.').max(100, 'At most 100 characters.'),
    scope: z.enum(['read_only', 'full']),
    ai_external: z.boolean(),
    expires_days: z
      .string()
      .trim()
      .refine(
        value =>
          value === '' || (/^\d+$/.test(value) && Number(value) >= 1 && Number(value) <= MAX_API_TOKEN_EXPIRES_DAYS),
        EXPIRES_DAYS_MESSAGE
      ),
  })
  .superRefine((data, ctx) => {
    if (data.scope === 'full' && data.ai_external) {
      ctx.addIssue({ code: 'custom', path: ['ai_external'], message: AI_EXTERNAL_REQUIRES_READ_ONLY });
    }
  })
  .transform(data => ({
    user_id: Number(data.user_id),
    label: data.label,
    scope: data.scope,
    // Always explicit, so what the Admin saw checked is exactly what is minted.
    ai_external: data.scope === 'read_only' ? data.ai_external : false,
    expires_days: data.expires_days === '' ? null : Number(data.expires_days),
  }));

export type ApiTokenIssueFormInput = z.input<typeof apiTokenIssueSchema>;
export type ApiTokenIssueFormData = z.output<typeof apiTokenIssueSchema>;
